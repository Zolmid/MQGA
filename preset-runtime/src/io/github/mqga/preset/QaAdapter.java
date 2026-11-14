package io.github.mqga.preset;

import android.content.Context;
import android.util.Log;

import de.robv.android.xposed.XC_MethodHook;
import de.robv.android.xposed.XposedBridge;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.RandomAccessFile;
import java.lang.reflect.Field;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.nio.channels.FileChannel;
import java.nio.channels.FileLock;
import java.nio.channels.OverlappingFileLockException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.IdentityHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

/**
 * Seeds the approved QA profile through the module's own typed configuration API.
 *
 * <p>The caller must verify the approved profile and supply the loader that actually owns QA's
 * ConfigManager, before StartupAgent runs. No module classes or native libraries are initialized
 * proactively. QA initializes MMKV inside NativeCoreBridge; its after callback then opens the
 * default store. Our getDefaultConfig after callback seeds it before the caller can read a switch,
 * including MainHook's early ForcePadMode initialization. Account stores are seeded on forAccount.
 *
 * <p>Both the store and the completion marker are scoped independently of the profile version.
 * An existing nonempty store is preserved as a whole, even when individual preset keys are absent.
 * This adapter neither accepts QA's EULA nor changes its initialization/feature gates.
 */
public final class QaAdapter {
    private static final String TAG = "MQGA-Preset";
    private static final String MODULE = "QAuxiliary";
    private static final String TYPE_SUFFIX = "$shadow$type";
    private static final String GLOBAL_PATH = "<host filesDir>/qa_mmkv/global_config";
    private static final String ACCOUNT_PATH = "<host filesDir>/qa_mmkv/u_<uin>";
    private static final String HOT_PATCH_PATH = "<host filesDir>/qn_disable_hot_patch";
    private static final Map<Class<?>, QaAdapter> INSTALLED = new IdentityHashMap<>();

    private final File filesDir;
    private final File stateDir;
    private final String profileId;
    private final List<Entry> globalEntries = new ArrayList<>();
    private final List<Entry> accountEntries = new ArrayList<>();
    private final Set<String> attempted = new HashSet<>();
    private final ThreadLocal<Boolean> insideSeed = new ThreadLocal<>();
    private final List<XC_MethodHook.Unhook> hooks = new ArrayList<>();
    private final Method getDefaultConfig;
    private final Method forAccount;
    private final Method nativeReady;
    private final Method getFile;
    private final Method getObject;
    private final Method getLong;
    private final Method contains;
    private final Method putBoolean;
    private final Method putString;
    private final Method putStringSet;
    private final Method remove;

    private QaAdapter(Context context, ClassLoader moduleLoader, JSONArray writes, String profileId)
            throws Exception {
        if (profileId == null || profileId.isEmpty() || profileId.length() > 128
                || profileId.indexOf('\n') >= 0 || profileId.indexOf('\r') >= 0) {
            throw new IllegalArgumentException("Invalid QA preset profile identifier");
        }
        this.profileId = profileId;
        filesDir = context.getFilesDir().getCanonicalFile();
        stateDir = new File(filesDir, "mqga-presets/" + MODULE);
        Class<?> configClass = Class.forName("io.github.qauxv.config.ConfigManager", false, moduleLoader);
        getDefaultConfig = configClass.getDeclaredMethod("getDefaultConfig");
        forAccount = configClass.getDeclaredMethod("forAccount", long.class);
        getFile = configClass.getMethod("getFile");
        getObject = configClass.getMethod("getObject", String.class);
        getLong = configClass.getMethod("getLong", String.class, long.class);
        contains = configClass.getMethod("contains", String.class);
        putBoolean = configClass.getMethod("putBoolean", String.class, boolean.class);
        putString = configClass.getMethod("putString", String.class, String.class);
        putStringSet = configClass.getMethod("putStringSet", String.class, Set.class);
        remove = configClass.getMethod("remove", String.class);
        Class<?> coreClass = Class.forName("io.github.qauxv.core.NativeCoreBridge", false, moduleLoader);
        nativeReady = coreClass.getDeclaredMethod("initializeMmkvForPrimaryNativeLibrary", Context.class);
        parse(writes);
    }

    /** Returns whether all lifecycle hooks are armed; failures never escape into the host. */
    public static synchronized boolean install(Context context, ClassLoader moduleLoader,
            JSONArray writes, String profileId) {
        QaAdapter adapter = null;
        try {
            if (context == null || moduleLoader == null || writes == null) {
                throw new IllegalArgumentException("Missing QA preset installation input");
            }
            if (!"com.tencent.mobileqq".equals(context.getPackageName())) {
                throw new IllegalArgumentException("QA preset is bound to the QQ host package");
            }
            Class<?> configClass = Class.forName("io.github.qauxv.config.ConfigManager", false, moduleLoader);
            if (INSTALLED.containsKey(configClass)) {
                return true;
            }
            adapter = new QaAdapter(context, moduleLoader, writes, profileId);
            adapter.armHooks();
            INSTALLED.put(configClass, adapter);
            Log.i(TAG, "QA preset lifecycle hooks installed");
            return true;
        } catch (Throwable error) {
            if (adapter != null) {
                adapter.unhookAll();
            }
            Log.e(TAG, "QA preset hooks could not be installed", error);
            return false;
        }
    }

    private void armHooks() {
        // After callbacks run in reverse priority order: seed before other after-read consumers.
        requireHook(getDefaultConfig, new XC_MethodHook(Integer.MIN_VALUE) {
            @Override
            protected void afterHookedMethod(MethodHookParam param) {
                if (!param.hasThrowable() && param.getResult() != null) {
                    seed(param.getResult(), 0L);
                }
            }
        });
        requireHook(forAccount, new XC_MethodHook(Integer.MIN_VALUE) {
            @Override
            protected void afterHookedMethod(MethodHookParam param) {
                if (!param.hasThrowable() && param.getResult() != null
                        && param.args[0] instanceof Long) {
                    long uin = (Long) param.args[0];
                    if (uin >= 10000L) {
                        seed(param.getResult(), uin);
                    }
                }
            }
        });
        requireHook(nativeReady, new XC_MethodHook(Integer.MIN_VALUE) {
            @Override
            protected void afterHookedMethod(MethodHookParam param) {
                if (!param.hasThrowable()) {
                    try {
                        // MMKV is now initialized by QA. This triggers the getDefaultConfig hook.
                        invoke(getDefaultConfig, null);
                    } catch (Throwable error) {
                        Log.e(TAG, "QA preset could not open the initialized default store", error);
                    }
                }
            }
        });
    }

    private void requireHook(Method method, XC_MethodHook callback) {
        XC_MethodHook.Unhook hook = XposedBridge.hookMethod(method, callback);
        if (hook == null) {
            throw new IllegalStateException("QA preset hook rejected: " + method.getName());
        }
        hooks.add(hook);
    }

    private void unhookAll() {
        for (XC_MethodHook.Unhook hook : hooks) {
            try {
                hook.unhook();
            } catch (Throwable error) {
                Log.e(TAG, "QA preset hook cleanup failed", error);
            }
        }
    }

    private void parse(JSONArray writes) throws Exception {
        Set<String> seen = new HashSet<>();
        for (int i = 0; i < writes.length(); i++) {
            JSONObject row = writes.getJSONObject(i);
            if (!MODULE.equals(row.getString("module"))) {
                throw new IllegalArgumentException("Unexpected module in QA preset writes");
            }
            JSONObject storage = row.getJSONObject("storage");
            String adapter = storage.getString("adapter");
            String key = storage.getString("key");
            String path = storage.getString("path");
            String type = storage.getString("type").toLowerCase(Locale.ROOT);
            boolean account = storage.getBoolean("account_bound");
            Object value = row.get("proposed_value");
            Entry entry;
            if ("host_file_marker".equals(adapter)) {
                if (account || !"file_marker".equals(type) || !HOT_PATCH_PATH.equals(path)
                        || !HOT_PATCH_PATH.equals(key) || !(value instanceof Boolean)) {
                    throw new IllegalArgumentException("Unsupported QA file-marker specification");
                }
                entry = new Entry(key, value, true);
            } else {
                if (!"qa_config_manager".equals(adapter)
                        || !(account ? ACCOUNT_PATH : GLOBAL_PATH).equals(path)
                        || key.isEmpty() || key.contains("$shadow$") || "uin".equals(key)) {
                    throw new IllegalArgumentException("Unsupported QA configuration scope or key");
                }
                if ("boolean".equals(type) && value instanceof Boolean) {
                    entry = new Entry(key, value, false);
                } else if ("string".equals(type) && value instanceof String) {
                    entry = new Entry(key, value, false);
                } else if ("set<string>".equals(type) && value instanceof JSONArray) {
                    JSONArray array = (JSONArray) value;
                    Set<String> set = new LinkedHashSet<>();
                    for (int j = 0; j < array.length(); j++) {
                        Object item = array.get(j);
                        if (!(item instanceof String)) {
                            throw new IllegalArgumentException("QA StringSet contains a non-string value");
                        }
                        set.add((String) item);
                    }
                    entry = new Entry(key, Collections.unmodifiableSet(set), false);
                } else {
                    throw new IllegalArgumentException("Unsupported QA preset value/type combination");
                }
            }
            if (!seen.add((account ? "account:" : "global:") + key)) {
                throw new IllegalArgumentException("Duplicate QA preset storage key");
            }
            (account ? accountEntries : globalEntries).add(entry);
        }
        if (globalEntries.isEmpty() && accountEntries.isEmpty()) {
            throw new IllegalArgumentException("Empty QA preset plan");
        }
    }

    // Original synchronized methods can release their monitor before Xposed's after callback.
    // A second reader must wait for this callback, not observe an in-progress attempted marker.
    private synchronized void seed(Object config, long uin) {
        if (Boolean.TRUE.equals(insideSeed.get())) {
            return;
        }
        List<Entry> entries = uin == 0L ? globalEntries : accountEntries;
        String scope = uin == 0L ? "global" : "u_" + uin;
        if (entries.isEmpty() || !attempted.add(scope)) {
            return;
        }
        insideSeed.set(Boolean.TRUE);
        try {
            ensureStateDirectory();
            try (RandomAccessFile file = new RandomAccessFile(new File(stateDir, scope + ".lock"), "rw");
                    FileChannel channel = file.getChannel();
                    FileLock ignored = acquireLock(channel)) {
                if (marker(scope, "done").exists() || marker(scope, "skipped").exists()) {
                    return;
                }
                File actualFile = (File) invoke(getFile, config);
                File expectedFile = new File(filesDir, "qa_mmkv/" + (uin == 0L ? "global_config" : "u_" + uin));
                if (actualFile == null || !actualFile.getCanonicalFile().equals(expectedFile.getCanonicalFile())) {
                    throw new IllegalStateException("QA configuration root does not match the host scope");
                }
                Object mmkv = mmkvOf(config);
                Method lock = mmkv.getClass().getMethod("lock");
                Method unlock = mmkv.getClass().getMethod("unlock");
                Method sync = mmkv.getClass().getMethod("sync");
                Method allKeys = mmkv.getClass().getMethod("allKeys");
                invoke(lock, mmkv);
                try {
                    invoke(mmkv.getClass().getMethod("checkContentChangedByOuterProcess"), mmkv);
                    if (!isPristine(config, (String[]) invoke(allKeys, mmkv), uin)) {
                        writeMarker(scope, "skipped", "preserved_existing_scope");
                        Log.i(TAG, "QA preset preserved an existing " + (uin == 0L ? "global" : "account") + " scope");
                        return;
                    }
                    applyToPristineScope(config, mmkv, sync, entries, scope);
                } finally {
                    invoke(unlock, mmkv);
                }
            }
        } catch (Throwable error) {
            recordFailure(scope, error);
        } finally {
            insideSeed.remove();
        }
    }

    private boolean isPristine(Object config, String[] keys, long uin) throws Throwable {
        if (keys != null) {
            for (String key : keys) {
                // ConfigManager.forAccount itself initializes this pair before its after callback.
                if (uin != 0L && ("uin".equals(key) || ("uin" + TYPE_SUFFIX).equals(key))) {
                    continue;
                }
                return false;
            }
        }
        if (uin != 0L) {
            return Boolean.TRUE.equals(invoke(contains, config, "uin"))
                    && Long.valueOf(uin).equals(invoke(getLong, config, "uin", 0L));
        }
        // Separate QA controls also count as existing user configuration, even with empty MMKV.
        return !new File(filesDir, "qn_disable_hot_patch").exists()
                && !new File(filesDir, "qa_target_dpi").exists()
                && !new File(filesDir, "qa_misc/qa_external_module_info.list").exists();
    }

    private void applyToPristineScope(Object config, Object mmkv, Method sync, List<Entry> entries,
            String scope) throws Throwable {
        List<Entry> touched = new ArrayList<>();
        File hotPatch = new File(filesDir, "qn_disable_hot_patch");
        try {
            for (Entry entry : entries) {
                if (entry.marker) {
                    if ((Boolean) entry.value) {
                        if (!hotPatch.createNewFile()) {
                            throw new IOException("QA file marker could not be created");
                        }
                        touched.add(entry);
                    } else if (hotPatch.exists()) {
                        throw new IOException("QA file marker changed during initialization");
                    }
                } else {
                    if (Boolean.TRUE.equals(invoke(contains, config, entry.key))) {
                        throw new IllegalStateException("QA target key became nonempty before seeding");
                    }
                    // Track before invoking: a setter may write the value and then fail on metadata.
                    touched.add(entry);
                    if (entry.value instanceof Boolean) {
                        invoke(putBoolean, config, entry.key, entry.value);
                    } else if (entry.value instanceof String) {
                        invoke(putString, config, entry.key, entry.value);
                    } else {
                        invoke(putStringSet, config, entry.key, entry.value);
                    }
                }
            }
            for (Entry entry : entries) {
                Object actual = entry.marker ? hotPatch.exists() : invoke(getObject, config, entry.key);
                if (!entry.value.equals(actual)) {
                    throw new IOException("QA preset typed readback failed");
                }
            }
            // ConfigManager.commit() is a no-op in this release. Use QA's own MMKV.sync().
            invoke(sync, mmkv);
            writeMarker(scope, "done", "verified_and_synced");
            Log.i(TAG, "QA preset initialized a pristine " + ("global".equals(scope) ? "global" : "account") + " scope");
        } catch (Throwable error) {
            try {
                for (Entry entry : touched) {
                    if (entry.marker) {
                        if (hotPatch.exists() && !hotPatch.delete()) {
                            throw new IOException("QA file-marker rollback failed");
                        }
                    } else {
                        invoke(remove, config, entry.key);
                    }
                }
                invoke(sync, mmkv);
            } catch (Throwable rollbackError) {
                Log.e(TAG, "QA preset rollback could not restore its newly written values", rollbackError);
            }
            throw error;
        }
    }

    private static Object mmkvOf(Object config) throws ReflectiveOperationException {
        for (Class<?> cls = config.getClass(); cls != null; cls = cls.getSuperclass()) {
            try {
                Field field = cls.getDeclaredField("mmkv");
                field.setAccessible(true);
                return field.get(config);
            } catch (NoSuchFieldException ignored) {
                // Search the owning implementation if a wrapper/subclass is present.
            }
        }
        throw new NoSuchFieldException("QA configuration implementation has no MMKV handle");
    }

    private void ensureStateDirectory() throws IOException {
        if (!stateDir.isDirectory() && !stateDir.mkdirs() && !stateDir.isDirectory()) {
            throw new IOException("Cannot create QA preset state directory");
        }
    }

    private File marker(String scope, String state) {
        return new File(stateDir, scope + "." + state);
    }

    private void writeMarker(String scope, String state, String detail) throws IOException {
        File target = marker(scope, state);
        File temporary = marker(scope, state + ".tmp");
        try {
            try (FileOutputStream output = new FileOutputStream(temporary)) {
                output.write(("profile=" + profileId + "\nstate=" + detail + "\n")
                        .getBytes(StandardCharsets.UTF_8));
                output.getFD().sync();
            }
            if (!temporary.renameTo(target)) {
                throw new IOException("Cannot commit QA preset state marker");
            }
        } finally {
            if (temporary.exists() && !temporary.delete()) {
                Log.w(TAG, "QA preset temporary state cleanup failed");
            }
        }
    }

    private void recordFailure(String scope, Throwable error) {
        Log.e(TAG, "QA preset initialization failed; no success marker is written", error);
        try {
            ensureStateDirectory();
            writeMarker(scope, "failed", error.getClass().getName());
        } catch (Throwable markerError) {
            Log.e(TAG, "QA preset failure status could not be persisted", markerError);
        }
    }

    private static FileLock acquireLock(FileChannel channel) throws IOException, InterruptedException {
        long deadline = System.nanoTime() + 3_000_000_000L;
        do {
            try {
                FileLock lock = channel.tryLock();
                if (lock != null) {
                    return lock;
                }
            } catch (OverlappingFileLockException ignored) {
                // Another thread/loader in this process owns the same scope lock.
            }
            try {
                Thread.sleep(10L);
            } catch (InterruptedException error) {
                Thread.currentThread().interrupt();
                throw error;
            }
        } while (System.nanoTime() < deadline);
        throw new IOException("Timed out waiting for QA preset scope initialization");
    }

    private static Object invoke(Method method, Object receiver, Object... args) throws Throwable {
        try {
            return method.invoke(receiver, args);
        } catch (InvocationTargetException error) {
            throw error.getCause() == null ? error : error.getCause();
        }
    }

    private static final class Entry {
        final String key;
        final Object value;
        final boolean marker;

        Entry(String key, Object value, boolean marker) {
            this.key = key;
            this.value = value;
            this.marker = marker;
        }
    }
}
