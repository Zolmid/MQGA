package io.github.mqga.preset;

import android.content.Context;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.RandomAccessFile;
import java.lang.reflect.Field;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.lang.reflect.Modifier;
import java.nio.channels.FileLock;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

import de.robv.android.xposed.XC_MethodHook;
import de.robv.android.xposed.XposedBridge;

/**
 * First-use adapter for the original XAutoDaily 3.0.37 (26070323) release APK.
 *
 * The build must pin SHA-256
 * ba91ec13f71beba82fd518aa60339a11eb5265445ea611edb9d9676799b5f7d0.
 * DEX names are rc (Config) and oc (ConfProxy), NOT JADX's synthetic names
 * defpackage.C1882rc and defpackage.C1691oc. No upstream APK is modified.
 *
 * rc.a() resolves the actual current account and returns its initialized
 * ConfProxy. Seeding in its after callback runs before the caller reads any
 * preference. oc.c("global_enable", ...) is a second barrier for the real
 * task executor (me.teble.xposed.autodaily.utils.l.j), including a cached proxy.
 * Neither hook eagerly initializes ZG, whose initializer requires QQ runtime.
 */
public final class XadAdapter {
    private static final String TAG = "MQGA-Preset";
    private static final String PREFIX = "XASettings-";
    private static final String MASTER = "global_enable";
    private static final Map<ClassLoader, XadAdapter> INSTALLED = new IdentityHashMap<>();
    private static final Set<String> TASKS = taskNames();
    private static final Set<String> VARIABLES = variableNames();

    private final File stateDirectory;
    private final String profileId;
    private final List<Write> writes;
    private final Field nativeStoreField;
    private final Field initializedField;
    private final Method mmapId;
    private final Method allKeys;
    private final Method nativeLock;
    private final Method nativeUnlock;
    private final Method refresh;
    private final Method encodeBoolean;
    private final Method encodeString;
    private final Method decodeBoolean;
    private final Method decodeString;
    private final Method contains;
    private final Method remove;
    private final Method sync;
    private final Object processLock = new Object();
    private final Set<String> terminalDomains = new HashSet<>();
    private final Set<String> failedDomains = new HashSet<>();
    private final Set<String> incompleteFreshDomains = new HashSet<>();
    private final ThreadLocal<Boolean> insideAdapter = new ThreadLocal<>();
    private volatile boolean armed;
    private XC_MethodHook.Unhook accountHook;
    private XC_MethodHook.Unhook masterHook;

    private XadAdapter(Context context, Class<?> config, Class<?> proxy,
                       JSONArray specification, String profile) throws Exception {
        stateDirectory = new File(context.getFilesDir(), "mqga-presets/XAutoDaily");
        profileId = profile == null ? "" : profile;
        writes = parseWrites(specification);
        nativeStoreField = proxy.getDeclaredField("a");
        nativeStoreField.setAccessible(true);
        Class<?> nativeStore = nativeStoreField.getType();
        if (!nativeStore.getName().equals("com.tencent.mmkv.MMKV")) {
            throw new IllegalStateException("Pinned ConfProxy storage field changed");
        }
        initializedField = config.getDeclaredField("b");
        initializedField.setAccessible(true);
        if (initializedField.getType() != boolean.class
                || !Modifier.isStatic(initializedField.getModifiers())) {
            throw new IllegalStateException("Pinned Config initialization flag changed");
        }
        mmapId = nativeStore.getMethod("mmapID");
        allKeys = nativeStore.getMethod("allKeys");
        nativeLock = nativeStore.getMethod("lock");
        nativeUnlock = nativeStore.getMethod("unlock");
        refresh = nativeStore.getMethod("checkContentChangedByOuterProcess");
        encodeBoolean = nativeStore.getMethod("encode", String.class, boolean.class);
        encodeString = nativeStore.getMethod("encode", String.class, String.class);
        decodeBoolean = nativeStore.getMethod("decodeBool", String.class, boolean.class);
        decodeString = nativeStore.getMethod("decodeString", String.class, String.class);
        contains = nativeStore.getMethod("containsKey", String.class);
        remove = nativeStore.getMethod("removeValueForKey", String.class);
        sync = nativeStore.getMethod("sync");
        if (mmapId.getReturnType() != String.class || allKeys.getReturnType() != String[].class
                || encodeBoolean.getReturnType() != boolean.class
                || encodeString.getReturnType() != boolean.class) {
            throw new IllegalStateException("Pinned MMKV API changed");
        }
    }

    /** Returns true only when both entry hooks are armed (including an earlier install). */
    public static boolean install(Context context, ClassLoader moduleLoader,
                                  JSONArray writes, String profileId) {
        synchronized (INSTALLED) {
            if (moduleLoader == null) return false;
            if (INSTALLED.containsKey(moduleLoader)) return true;
            if (context == null || writes == null || writes.length() == 0) return false;
            XadAdapter adapter = null;
            try {
                Class<?> config = Class.forName("rc", false, moduleLoader);
                Class<?> proxy = Class.forName("oc", false, moduleLoader);
                Method accountGetter = config.getDeclaredMethod("a");
                Method booleanGetter = proxy.getDeclaredMethod("c", String.class, boolean.class);
                if (accountGetter.getReturnType() != proxy
                        || Modifier.isStatic(accountGetter.getModifiers())
                        || booleanGetter.getReturnType() != boolean.class) {
                    throw new IllegalStateException("Pinned XAutoDaily entry points changed");
                }
                adapter = new XadAdapter(context, config, proxy, writes, profileId);
                final XadAdapter active = adapter;
                // After callbacks execute in reverse priority order. Seed before
                // any other after callback can consume the returned proxy.
                adapter.accountHook = XposedBridge.hookMethod(accountGetter,
                        new XC_MethodHook(Integer.MIN_VALUE) {
                            @Override
                            protected void afterHookedMethod(MethodHookParam param) {
                                if (active.armed && !param.hasThrowable()) {
                                    active.seedSafely(param.getResult());
                                }
                            }
                        });
                if (adapter.accountHook == null) {
                    throw new IllegalStateException("Xposed did not arm the account getter hook");
                }
                adapter.masterHook = XposedBridge.hookMethod(booleanGetter,
                        new XC_MethodHook(Integer.MAX_VALUE) {
                            @Override
                            protected void beforeHookedMethod(MethodHookParam param) {
                                if (active.armed && param.args.length == 2 && MASTER.equals(param.args[0])) {
                                    active.seedSafely(param.thisObject);
                                    if (active.hasIncompleteFreshSeed(param.thisObject)) {
                                        param.setResult(false);
                                    }
                                }
                            }
                        });
                if (adapter.masterHook == null) {
                    throw new IllegalStateException("Xposed did not arm the master preference hook");
                }
                adapter.armed = true;
                Log.i(TAG, "XAutoDaily: pinned 3.0.37 account hooks installed");
                INSTALLED.put(moduleLoader, adapter);
                return true;
            } catch (Throwable failure) {
                INSTALLED.remove(moduleLoader);
                if (adapter != null) {
                    adapter.armed = false;
                    removePartialHook(adapter.masterHook);
                    removePartialHook(adapter.accountHook);
                }
                logFailure("XAutoDaily: adapter not installed; settings unchanged", failure);
                return false;
            }
        }
    }

    private static void removePartialHook(XC_MethodHook.Unhook hook) {
        if (hook == null) return;
        try {
            hook.unhook();
        } catch (Throwable cleanupFailure) {
            // Attempt the other unhook even if this cleanup fails.
            logFailure("XAutoDaily: partial hook cleanup failed", cleanupFailure);
        }
    }

    private void seedSafely(Object proxy) {
        if (proxy == null || Boolean.TRUE.equals(insideAdapter.get())) return;
        insideAdapter.set(Boolean.TRUE);
        String domain = null;
        try {
            // Reading this only inside a callback avoids premature rc/ZG setup.
            if (!initializedField.getBoolean(null)) return;
            Object store = nativeStoreField.get(proxy);
            if (store == null) return;
            String id = (String) mmapId.invoke(store);
            if (!isValidAccountStore(id)) return;
            domain = digest(id);
            synchronized (processLock) {
                if (terminalDomains.contains(domain) || failedDomains.contains(domain)) return;
                ensureDirectory(stateDirectory);
                File lockFile = new File(stateDirectory, domain + ".lock");
                // The domain is stable across profile versions. Upgrading the
                // preset cannot resurrect settings after the user disables them.
                try (RandomAccessFile lock = new RandomAccessFile(lockFile, "rw");
                     FileLock ignored = lock.getChannel().lock()) {
                    File done = new File(stateDirectory, domain + ".done");
                    File skipped = new File(stateDirectory, domain + ".skipped");
                    if (done.exists() || skipped.exists()) {
                        terminalDomains.add(domain);
                        return;
                    }
                    nativeLock.invoke(store);
                    try {
                        refresh.invoke(store);
                        String[] previousKeys = (String[]) allKeys.invoke(store);
                        // The whole account domain must be empty, not merely
                        // our proposed keys. In particular task#enable/env may
                        // exist while global_enable is still absent/false.
                        if (previousKeys != null && previousKeys.length != 0) {
                            writeMarker(skipped, domain, "existing-account-domain");
                            terminalDomains.add(domain);
                            Log.i(TAG, "XAutoDaily: existing account configuration preserved");
                            return;
                        }
                        applyFreshStore(store, done, domain);
                        terminalDomains.add(domain);
                        Log.i(TAG, "XAutoDaily: fresh account preset committed and verified");
                    } finally {
                        nativeUnlock.invoke(store);
                    }
                }
            }
        } catch (Throwable failure) {
            if (domain != null) {
                synchronized (processLock) {
                    failedDomains.add(domain);
                }
            }
            // An adapter failure must never escape into QQ or be marked done.
            logFailure("XAutoDaily: seeding stopped; no completion marker", failure);
        } finally {
            insideAdapter.remove();
        }
    }

    private void applyFreshStore(Object store, File done, String domain) throws Throwable {
        List<String> attempted = new ArrayList<>();
        try {
            for (Write write : writes) {
                // Include the key before invoking JNI: a throwing call may have
                // performed its write. All keys were absent under the MMKV lock.
                attempted.add(write.key);
                Method encoder = write.value instanceof Boolean ? encodeBoolean : encodeString;
                if (!Boolean.TRUE.equals(encoder.invoke(store, write.key, write.value))) {
                    throw new IOException("MMKV rejected an XAutoDaily preset value");
                }
                verify(store, write);
                // Parameters are sorted first, task enables next, master last.
                // A missing master stays false if interrupted before this point.
            }
            sync.invoke(store);
            for (Write write : writes) verify(store, write);
            writeMarker(done, domain, "applied");
        } catch (Throwable failure) {
            // Fail closed for this previously empty domain in this process if
            // JNI or disk failure prevents a complete rollback. Existing user
            // domains never enter this set and their master value is untouched.
            incompleteFreshDomains.add(domain);
            // No pre-existing values were present. Undo only our attempted keys,
            // with the master first, rather than clearing the whole MMKV store.
            try {
                if (attempted.contains(MASTER)) remove.invoke(store, MASTER);
                for (String key : attempted) {
                    if (!MASTER.equals(key)) remove.invoke(store, key);
                }
                sync.invoke(store);
            } catch (Throwable rollbackFailure) {
                failure.addSuppressed(unwrap(rollbackFailure));
            }
            throw failure;
        }
    }

    private boolean hasIncompleteFreshSeed(Object proxy) {
        try {
            Object store = nativeStoreField.get(proxy);
            if (store == null) return false;
            String id = (String) mmapId.invoke(store);
            if (!isValidAccountStore(id)) return false;
            synchronized (processLock) {
                return incompleteFreshDomains.contains(digest(id));
            }
        } catch (Throwable ignored) {
            return false;
        }
    }

    private void verify(Object store, Write write) throws Exception {
        if (!Boolean.TRUE.equals(contains.invoke(store, write.key))) {
            throw new IOException("MMKV did not retain an XAutoDaily preset key");
        }
        Object actual = write.value instanceof Boolean
                ? decodeBoolean.invoke(store, write.key, !((Boolean) write.value))
                : decodeString.invoke(store, write.key, (Object) null);
        if (!write.value.equals(actual)) {
            throw new IOException("XAutoDaily preset readback mismatch");
        }
    }

    private void writeMarker(File target, String domain, String status) throws Exception {
        JSONObject document = new JSONObject();
        document.put("schema", 1);
        document.put("module", "XAutoDaily");
        document.put("domain", domain);
        document.put("profile", profileId);
        document.put("status", status);
        document.put("time", System.currentTimeMillis());
        File temporary = new File(target.getPath() + ".tmp");
        try {
            try (FileOutputStream output = new FileOutputStream(temporary)) {
                output.write(document.toString().getBytes(StandardCharsets.UTF_8));
                output.flush();
                output.getFD().sync();
            }
            if (!temporary.renameTo(target)) {
                throw new IOException("Cannot commit XAutoDaily preset marker");
            }
        } finally {
            if (temporary.exists() && !temporary.delete()) {
                Log.w(TAG, "XAutoDaily: could not remove temporary state marker");
            }
        }
    }

    private static List<Write> parseWrites(JSONArray records) throws Exception {
        Map<String, Write> unique = new HashMap<>();
        for (int index = 0; index < records.length(); index++) {
            JSONObject record = records.getJSONObject(index);
            JSONObject storage = record.getJSONObject("storage");
            String key = storage.getString("key");
            String type = storage.getString("type");
            Object value = record.get("proposed_value");
            if (!"XAutoDaily".equals(record.getString("module"))
                    || !"xad_confproxy_mmkv".equals(storage.getString("adapter"))
                    || !storage.getBoolean("account_bound")
                    || !"<host filesDir>/xa_mmkv/XASettings-<runtime UIN>".equals(storage.getString("path"))) {
                throw new IllegalArgumentException("XAutoDaily preset storage scope mismatch");
            }
            boolean booleanKey = MASTER.equals(key) || TASKS.contains(key);
            if (booleanKey) {
                if (!"Boolean".equals(type) || !(value instanceof Boolean)) {
                    throw new IllegalArgumentException("XAutoDaily Boolean type mismatch");
                }
            } else if (VARIABLES.contains(key)) {
                if (!"String".equals(type) || !(value instanceof String)) {
                    throw new IllegalArgumentException("XAutoDaily variable must be a String");
                }
                validateVariable(key, (String) value);
            } else {
                throw new IllegalArgumentException("Unreviewed XAutoDaily key");
            }
            if (unique.put(key, new Write(key, value)) != null) {
                throw new IllegalArgumentException("Duplicate XAutoDaily key");
            }
        }
        requireWhenEnabled(unique, "群打卡", "groups", "hour", "minute");
        requireWhenEnabled(unique, "好友点赞", "friends");
        requireWhenEnabled(unique, "资料卡回赞", "maxPage", "maxDays");
        requireWhenEnabled(unique, "QQ运动步数", "steps");
        // The reviewed preset does not configure messaging/keep-fire targets.
        if (enabled(unique, "群组续火#enable") || enabled(unique, "好友续火花#enable")) {
            throw new IllegalArgumentException("Keep-fire tasks are outside this preset");
        }
        if (enabled(unique, MASTER)) {
            boolean readyTask = false;
            for (String key : TASKS) readyTask |= enabled(unique, key);
            if (!readyTask) throw new IllegalArgumentException("XAutoDaily master has no ready task");
        }
        List<Write> result = new ArrayList<>(unique.values());
        Collections.sort(result, new Comparator<Write>() {
            @Override
            public int compare(Write left, Write right) {
                int stage = Integer.compare(left.stage(), right.stage());
                return stage != 0 ? stage : left.key.compareTo(right.key);
            }
        });
        return Collections.unmodifiableList(result);
    }

    private static void validateVariable(String key, String value) {
        if (key.endsWith("#groups") || key.endsWith("#friends")) {
            if (!value.matches("[1-9][0-9]*(,[1-9][0-9]*)*")) {
                throw new IllegalArgumentException("XAutoDaily target list is empty or invalid");
            }
            for (String number : value.split(",")) {
                if (Long.parseLong(number) <= 0) throw new IllegalArgumentException("Invalid target");
            }
            return;
        }
        if (!value.matches("[0-9]+")) throw new IllegalArgumentException("Invalid numeric variable");
        int number = Integer.parseInt(value);
        int limit = key.endsWith("#hour") ? 23 : key.endsWith("#minute") ? 59
                : key.endsWith("#steps") ? 99999 : Integer.MAX_VALUE;
        if (number > limit || (key.endsWith("#maxPage") && number == 0)) {
            throw new IllegalArgumentException("XAutoDaily variable is outside its allowed range");
        }
    }

    private static void requireWhenEnabled(Map<String, Write> writes, String task, String... names) {
        if (!enabled(writes, task + "#enable")) return;
        for (String name : names) {
            if (!writes.containsKey(task + "#env_variable#" + name)) {
                throw new IllegalArgumentException("Enabled XAutoDaily task is missing parameters");
            }
        }
    }

    private static boolean enabled(Map<String, Write> writes, String key) {
        Write write = writes.get(key);
        return write != null && Boolean.TRUE.equals(write.value);
    }

    private static boolean isValidAccountStore(String id) {
        if (id == null || !id.startsWith(PREFIX)) return false;
        String number = id.substring(PREFIX.length());
        if (!number.matches("[1-9][0-9]*")) return false;
        try {
            return Long.parseLong(number) > 0;
        } catch (NumberFormatException ignored) {
            return false;
        }
    }

    private static Set<String> taskNames() {
        Set<String> result = new HashSet<>();
        for (String task : new String[]{"群打卡", "好友点赞", "资料卡回赞", "QQ运动步数", "群组续火", "好友续火花"}) {
            result.add(task + "#enable");
        }
        return Collections.unmodifiableSet(result);
    }

    private static Set<String> variableNames() {
        Set<String> result = new HashSet<>();
        for (String suffix : new String[]{"groups", "hour", "minute"}) {
            result.add("群打卡#env_variable#" + suffix);
        }
        result.add("好友点赞#env_variable#friends");
        result.add("资料卡回赞#env_variable#maxPage");
        result.add("资料卡回赞#env_variable#maxDays");
        result.add("QQ运动步数#env_variable#steps");
        return Collections.unmodifiableSet(result);
    }

    private static void ensureDirectory(File directory) throws IOException {
        if (!directory.isDirectory() && !directory.mkdirs() && !directory.isDirectory()) {
            throw new IOException("Cannot create XAutoDaily preset state directory");
        }
    }

    private static String digest(String value) throws Exception {
        byte[] bytes = MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8));
        StringBuilder result = new StringBuilder(bytes.length * 2);
        for (byte item : bytes) {
            result.append(Character.forDigit((item >>> 4) & 15, 16));
            result.append(Character.forDigit(item & 15, 16));
        }
        return result.toString();
    }

    private static Throwable unwrap(Throwable failure) {
        while (failure instanceof InvocationTargetException
                && ((InvocationTargetException) failure).getCause() != null) {
            failure = ((InvocationTargetException) failure).getCause();
        }
        return failure;
    }

    private static void logFailure(String operation, Throwable failure) {
        // Account ids, target lists and preference values are never logged.
        Log.e(TAG, operation + " (" + unwrap(failure).getClass().getSimpleName() + ")");
    }

    private static final class Write {
        final String key;
        final Object value;

        Write(String key, Object value) {
            this.key = key;
            this.value = value;
        }

        int stage() {
            return MASTER.equals(key) ? 2 : value instanceof String ? 0 : 1;
        }
    }
}
