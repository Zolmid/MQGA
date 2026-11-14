package io.github.mqga.preset;

import android.content.Context;
import android.content.SharedPreferences;
import android.os.Environment;
import android.util.AtomicFile;
import android.util.Log;

import de.robv.android.xposed.XC_MethodHook;
import de.robv.android.xposed.XposedBridge;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.RandomAccessFile;
import java.lang.reflect.Constructor;
import java.lang.reflect.Method;
import java.nio.channels.FileChannel;
import java.nio.channels.FileLock;
import java.nio.charset.StandardCharsets;
import java.util.HashSet;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;
import java.util.WeakHashMap;

/**
 * Configuration-only adapter for the reviewed, unmodified QWish 3.0.6 APK.
 * The build must pin its APK hash: the Unicode FastKV builder name below is
 * the real DEX name, not JADX's synthetic p337.C5036 alias.
 * No eligibility, license, or native feature checks are modified.
 */
public final class QWishAdapter {
    private static final String TAG = "MQGA-Preset";
    private static final String BUILDER_CLASS = "清清清清清秋清梦.清清清清清清思若";
    private static final String BUILD_METHOD = "清清清清清清清清";
    private static final Map<ClassLoader, State> INSTALLED = new WeakHashMap<>();

    private QWishAdapter() {}

    public static boolean install(Context context, ClassLoader moduleLoader,
                               JSONArray writes, String profileId) {
        synchronized (INSTALLED) {
            if (INSTALLED.containsKey(moduleLoader)) return true;
            try {
                State state = new State(context, moduleLoader, writes, profileId);
                if (state.features.length() == 0 && state.stores.isEmpty()) return true;
                state.install();
                INSTALLED.put(moduleLoader, state);
                Log.i(TAG, "QWish defaults armed before CoreLoader configuration read");
                return true;
            } catch (Throwable error) {
                Log.e(TAG, "QWish adapter could not be installed; no defaults applied", error);
                return false;
            }
        }
    }

    private static final class Value {
        final String type;
        final Object value;

        Value(String type, Object raw, boolean jsonStore) throws Exception {
            this.type = type;
            if ("Boolean".equals(type) && raw instanceof Boolean) value = raw;
            else if ("String".equals(type) && raw instanceof String) value = raw;
            else if ("Int".equals(type) && integer(raw, Integer.MIN_VALUE, Integer.MAX_VALUE)) {
                value = Integer.valueOf(((Number) raw).intValue());
            } else if ("Long".equals(type) && integer(raw, Long.MIN_VALUE, Long.MAX_VALUE)) {
                value = Long.valueOf(((Number) raw).longValue());
            } else if ("Float".equals(type) && raw instanceof Number
                    && !Float.isNaN(((Number) raw).floatValue())
                    && !Float.isInfinite(((Number) raw).floatValue())) {
                value = Float.valueOf(((Number) raw).floatValue());
            } else if (jsonStore && "Double".equals(type) && raw instanceof Number) {
                value = raw;
            } else if ("Set<String>".equals(type) && raw instanceof JSONArray) {
                JSONArray array = (JSONArray) raw;
                Set<String> result = new HashSet<>();
                for (int index = 0; index < array.length(); index++) {
                    if (!(array.get(index) instanceof String)) throw new IOException("Non-string set member");
                    result.add(array.getString(index));
                }
                value = result;
            } else {
                throw new IOException("Unsupported typed QWish parameter");
            }
        }

        Object jsonValue() {
            if (value instanceof Set) return new JSONArray((Set<?>) value);
            return value;
        }

        @SuppressWarnings("unchecked")
        void put(SharedPreferences.Editor edit, String key) throws IOException {
            if (value instanceof Boolean) edit.putBoolean(key, (Boolean) value);
            else if (value instanceof Integer) edit.putInt(key, (Integer) value);
            else if (value instanceof Long) edit.putLong(key, (Long) value);
            else if (value instanceof Float) edit.putFloat(key, (Float) value);
            else if (value instanceof String) edit.putString(key, (String) value);
            else if (value instanceof Set) edit.putStringSet(key, (Set<String>) value);
            else throw new IOException("No SharedPreferences method for QWish value type");
        }

        boolean matches(Object candidate) {
            // FastKV is typed: Float must not silently become a double/string.
            return value.equals(candidate);
        }
    }

    private static final class Store {
        final String directory;
        final String name;
        final boolean json;
        final Map<String, Value> values = new LinkedHashMap<>();

        Store(String directory, String name, boolean json) {
            this.directory = directory;
            this.name = name;
            this.json = json;
        }

        JSONObject jsonObject() throws Exception {
            JSONObject object = new JSONObject();
            for (Map.Entry<String, Value> entry : values.entrySet()) {
                object.put(entry.getKey(), entry.getValue().jsonValue());
            }
            return object;
        }

        boolean compatible(Map<String, ?> existing, boolean recovering) {
            if (!recovering) return existing.isEmpty();
            for (Map.Entry<String, ?> entry : existing.entrySet()) {
                Value value = values.get(entry.getKey());
                if (value == null || !value.matches(entry.getValue())) return false;
            }
            return true;
        }
    }

    private static final class State {
        final Context context;
        final ClassLoader loader;
        final String profileId;
        final JSONObject features = new JSONObject();
        final Map<String, Store> stores = new LinkedHashMap<>();
        final ThreadLocal<Boolean> inside = new ThreadLocal<>();
        final File markerRoot;
        boolean completed;
        Constructor<?> builderConstructor;
        Method buildMethod;

        State(Context context, ClassLoader loader, JSONArray writes, String profileId) throws Exception {
            this.context = context;
            this.loader = loader;
            this.profileId = profileId;
            if (profileId == null || profileId.isEmpty()) throw new IOException("Missing profile identity");
            markerRoot = new File(context.getFilesDir(), "mqga-presets/QWish");
            for (int index = 0; index < writes.length(); index++) {
                JSONObject write = writes.getJSONObject(index);
                if (!"QWish".equals(write.optString("module"))) continue;
                JSONObject storage = write.getJSONObject("storage");
                String key = storage.getString("key");
                String adapter = storage.getString("adapter");
                Object raw = write.get("proposed_value");
                if ("qwish_feature_map".equals(adapter)) {
                    JSONObject fields = storage.getJSONObject("entry_fields");
                    if (!(raw instanceof Boolean) || !"Boolean".equals(storage.getString("type"))
                            || !key.equals(fields.getString("itemPath"))
                            || !Boolean.TRUE.equals(fields.get("bypassDefaultLoad"))
                            || features.has(key)) {
                        throw new IOException("Invalid QWish feature map specification");
                    }
                    JSONObject feature = new JSONObject();
                    feature.put("itemPath", key);
                    feature.put("enabled", raw);
                    feature.put("bypassDefaultLoad", true);
                    features.put(key, feature);
                } else if ("qwish_parameter".equals(adapter)) {
                    JSONObject upstream = storage.getJSONObject("upstream_storage");
                    String kind = upstream.getString("kind");
                    String path = upstream.getString("path");
                    String name = upstream.getString("store");
                    boolean json = "JSON".equals(kind);
                    String directory;
                    if (json && path.endsWith("/QWish/data/simple")) directory = "data/simple";
                    else if (("FastKV".equals(kind) || "FastKV-delegate".equals(kind))
                            && path.endsWith("/QWish/config")) directory = "config";
                    else if (("FastKV".equals(kind) || "FastKV-delegate".equals(kind))
                            && path.endsWith("/QWish/data/xphelper")) directory = "data/xphelper";
                    else throw new IOException("Unsupported QWish storage location");
                    if (name.isEmpty() || name.contains("/") || name.contains("\\")
                            || ".".equals(name) || "..".equals(name)) {
                        throw new IOException("Invalid QWish store name");
                    }
                    String identity = directory + "/" + name;
                    Store store = stores.get(identity);
                    if (store == null) {
                        store = new Store(directory, name, json);
                        stores.put(identity, store);
                    }
                    if (store.json != json || store.values.containsKey(key)) {
                        throw new IOException("Duplicate QWish parameter target");
                    }
                    store.values.put(key, new Value(storage.getString("type"), raw, json));
                } else {
                    throw new IOException("Unsupported QWish adapter");
                }
            }
        }

        void install() throws Exception {
            Class<?> core = Class.forName("top.qwish.xposed.loader.CoreLoader", false, loader);
            Method load = core.getDeclaredMethod("loadHookItemConfig");
            XC_MethodHook.Unhook hook = XposedBridge.hookMethod(load, new XC_MethodHook(Integer.MAX_VALUE) {
                @Override protected void beforeHookedMethod(MethodHookParam param) {
                    if (!beforeLoad()) param.setResult(null);
                }
            });
            if (hook == null) throw new IOException("QWish configuration hook was rejected");
        }

        synchronized boolean beforeLoad() {
            if (completed || Boolean.TRUE.equals(inside.get())) return true;
            inside.set(Boolean.TRUE);
            try {
                seed();
                return true;
            } catch (Throwable error) {
                Log.w(TAG, "QWish defaults deferred; no completion marker written", error);
                // A partial new-scope transaction must not enable features or let
                // upstream persist an empty default map ahead of its parameters.
                // Existing settings and all native eligibility checks are untouched.
                return !new File(markerRoot, "pending").isFile();
            } finally {
                inside.remove();
            }
        }

        void seed() throws Exception {
            mkdirs(markerRoot);
            try (RandomAccessFile handle = new RandomAccessFile(new File(markerRoot, "lock"), "rw");
                 FileChannel channel = handle.getChannel(); FileLock lock = channel.lock()) {
                File done = new File(markerRoot, "done");
                File skipped = new File(markerRoot, "skipped");
                File pending = new File(markerRoot, "pending");
                if (done.isFile() || skipped.isFile()) {
                    completed = true;
                    return;
                }
                File root = new File(Environment.getExternalStorageDirectory(),
                        "Android/data/" + context.getPackageName() + "/QWish");
                File legacy = new File(root, "data/item_config.json");
                boolean recovering = pending.isFile();
                if (recovering && !profileId.equals(readObject(pending).optString("profileId"))) {
                    mark(skipped, "different pending profile");
                    completed = true;
                    return;
                }
                // C0576 has initialized QWish's host context by this stable Java
                // boundary. Opening its original FastKV builder now uses the same
                // cached store instance CoreLoader will subsequently consume.
                SharedPreferences main = preferences(new File(root, "data/xphelper"), "hook_item_config");
                if (!mainCompatible(main, recovering) || !legacyCompatible(legacy, recovering)
                        || !childrenCompatible(root, recovering)) {
                    mark(skipped, "existing QWish configuration");
                    completed = true;
                    Log.i(TAG, "QWish existing configuration preserved");
                    return;
                }
                if (!recovering) mark(pending, "parameters before feature map");
                for (Store store : stores.values()) {
                    if (store.json) {
                        File file = new File(new File(root, store.directory), store.name);
                        JSONObject object = store.jsonObject();
                        if (!file.isFile() || file.length() == 0 || readObject(file).length() == 0) {
                            writeAtomic(file, object.toString());
                        }
                        if (!jsonEqual(readObject(file), object)) {
                            throw new IOException("QWish JSON parameter readback failed");
                        }
                    } else {
                        SharedPreferences preferences = preferences(new File(root, store.directory), store.name);
                        SharedPreferences.Editor edit = preferences.edit();
                        for (Map.Entry<String, Value> entry : store.values.entrySet()) {
                            if (!preferences.contains(entry.getKey())) entry.getValue().put(edit, entry.getKey());
                        }
                        if (!edit.commit()) throw new IOException("QWish FastKV commit failed");
                        Map<String, ?> values = preferences.getAll();
                        for (Map.Entry<String, Value> entry : store.values.entrySet()) {
                            if (!entry.getValue().matches(values.get(entry.getKey()))) {
                                throw new IOException("QWish typed FastKV readback failed");
                            }
                        }
                    }
                }
                // Never manufacture a FastKV file. Upstream CoreLoader's existing
                // migration reads this map when `items` is absent and persists it.
                if (!legacy.isFile() || legacy.length() == 0) writeAtomic(legacy, features.toString());
                if (!jsonEqual(readObject(legacy), features)) throw new IOException("QWish map readback failed");
                mark(done, "applied; upstream migration follows");
                pending.delete();
                completed = true;
                Log.i(TAG, "QWish child parameters prepared before first feature-map load");
            }
        }

        SharedPreferences preferences(File directory, String name) throws Exception {
            if (builderConstructor == null) {
                Class<?> builder = Class.forName(BUILDER_CLASS, true, loader);
                builderConstructor = builder.getDeclaredConstructor(String.class, String.class);
                builderConstructor.setAccessible(true);
                buildMethod = builder.getDeclaredMethod(BUILD_METHOD);
                buildMethod.setAccessible(true);
            }
            Object builder = builderConstructor.newInstance(directory.getAbsolutePath(), name);
            Object result = buildMethod.invoke(builder);
            if (!(result instanceof SharedPreferences)) throw new IOException("Unexpected QWish FastKV API");
            return (SharedPreferences) result;
        }

        boolean mainCompatible(SharedPreferences preferences, boolean recovering) throws Exception {
            Map<String, ?> values = preferences.getAll();
            if (values.isEmpty()) return true;
            if (!recovering || values.size() != 1 || !values.containsKey("items")) return false;
            Object stored = values.get("items");
            return stored instanceof String && jsonEqual(new JSONObject((String) stored), features);
        }

        boolean legacyCompatible(File legacy, boolean recovering) throws Exception {
            if (!legacy.exists()) return true;
            if (!legacy.isFile()) return false;
            if (legacy.length() == 0) return true;
            return recovering && jsonEqual(readObject(legacy), features);
        }

        boolean childrenCompatible(File root, boolean recovering) throws Exception {
            for (Store store : stores.values()) {
                if (store.json) {
                    File file = new File(new File(root, store.directory), store.name);
                    if (file.exists() && !file.isFile()) return false;
                    if (file.isFile() && file.length() > 0) {
                        JSONObject object = readObject(file);
                        if (object.length() > 0 && (!recovering || !jsonEqual(object, store.jsonObject()))) return false;
                    }
                } else if (!store.compatible(preferences(new File(root, store.directory), store.name).getAll(), recovering)) {
                    return false;
                }
            }
            // Main items normally establishes whether a module was configured.
            // Also preserve orphaned user settings left by backups/migrations.
            File simple = new File(root, "data/simple");
            if (simple.exists()) {
                File[] files = simple.listFiles();
                if (files == null) throw new IOException("Cannot inspect QWish simple settings");
                for (File file : files) {
                    String name = file.getName();
                    if (stores.containsKey("data/simple/" + name)) continue;
                    if (recovering && (name.endsWith(".new") || name.endsWith(".bak"))
                            && stores.containsKey("data/simple/" + name.substring(0, name.length() - 4))) continue;
                    if (file.isDirectory()) {
                        if (hasContent(file)) return false;
                    } else if (file.length() > 0 && readObject(file).length() > 0) return false;
                }
            }
            File config = new File(root, "config");
            if (config.exists()) {
                File[] files = config.listFiles();
                if (files == null) throw new IOException("Cannot inspect QWish delegate settings");
                Set<String> inspected = new HashSet<>();
                for (File file : files) {
                    String name = file.getName();
                    if (file.isDirectory()) {
                        if (hasContent(file)) return false;
                        continue;
                    }
                    if (!(name.endsWith(".kva") || name.endsWith(".kvb")
                            || name.endsWith(".kvc") || name.endsWith(".tmp"))) {
                        if (file.length() > 0) return false;
                        continue;
                    }
                    String storeName = name.substring(0, name.length() - 4);
                    if (stores.containsKey("config/" + storeName) || !inspected.add(storeName)) continue;
                    if (!preferences(config, storeName).getAll().isEmpty()) return false;
                }
            }
            return true;
        }

        void mark(File file, String state) throws Exception {
            JSONObject marker = new JSONObject();
            marker.put("profileId", profileId);
            marker.put("state", state);
            marker.put("time", System.currentTimeMillis());
            writeAtomic(file, marker.toString());
        }
    }

    private static boolean integer(Object value, long minimum, long maximum) {
        if (!(value instanceof Number)) return false;
        double number = ((Number) value).doubleValue();
        return !Double.isNaN(number) && !Double.isInfinite(number)
                && number == Math.rint(number) && number >= minimum && number <= maximum;
    }

    private static boolean jsonEqual(Object first, Object second) throws Exception {
        if (first instanceof JSONObject && second instanceof JSONObject) {
            JSONObject left = (JSONObject) first, right = (JSONObject) second;
            if (left.length() != right.length()) return false;
            Iterator<String> keys = left.keys();
            while (keys.hasNext()) {
                String key = keys.next();
                if (!right.has(key) || !jsonEqual(left.get(key), right.get(key))) return false;
            }
            return true;
        }
        if (first instanceof JSONArray && second instanceof JSONArray) {
            JSONArray left = (JSONArray) first, right = (JSONArray) second;
            if (left.length() != right.length()) return false;
            for (int index = 0; index < left.length(); index++) {
                if (!jsonEqual(left.get(index), right.get(index))) return false;
            }
            return true;
        }
        if (first instanceof Number && second instanceof Number) {
            return Double.compare(((Number) first).doubleValue(), ((Number) second).doubleValue()) == 0;
        }
        return first == null ? second == null : first.equals(second);
    }

    private static boolean hasContent(File directory) throws IOException {
        File[] files = directory.listFiles();
        if (files == null) throw new IOException("Cannot inspect existing QWish directory");
        for (File file : files) {
            if (file.isDirectory() ? hasContent(file) : file.length() > 0) return true;
        }
        return false;
    }

    private static JSONObject readObject(File file) throws Exception {
        return new JSONObject(readText(file));
    }

    private static String readText(File file) throws IOException {
        try (FileInputStream input = new FileInputStream(file);
             ByteArrayOutputStream bytes = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[4096];
            for (int count; (count = input.read(buffer)) != -1;) {
                bytes.write(buffer, 0, count);
                if (bytes.size() > 1048576) throw new IOException("Oversized QWish configuration");
            }
            return new String(bytes.toByteArray(), StandardCharsets.UTF_8);
        }
    }

    private static void mkdirs(File directory) throws IOException {
        if (!directory.isDirectory() && !directory.mkdirs() && !directory.isDirectory()) {
            throw new IOException("Cannot create preset configuration directory");
        }
    }

    private static void writeAtomic(File file, String text) throws IOException {
        mkdirs(file.getParentFile());
        AtomicFile atomic = new AtomicFile(file);
        FileOutputStream output = null;
        try {
            output = atomic.startWrite();
            output.write(text.getBytes(StandardCharsets.UTF_8));
            atomic.finishWrite(output);
            output = null;
            if (!file.isFile() || !text.equals(readText(file))) {
                throw new IOException("Atomic preset write did not persist");
            }
        } catch (IOException | RuntimeException error) {
            if (output != null) atomic.failWrite(output);
            throw error;
        }
    }
}
