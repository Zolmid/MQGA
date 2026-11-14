package io.github.mqga.preset;

import android.content.Context;
import android.content.SharedPreferences;
import android.os.SystemClock;
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
import java.lang.reflect.Method;
import java.nio.channels.FileChannel;
import java.nio.channels.FileLock;
import java.nio.charset.StandardCharsets;
import java.util.Collections;
import java.util.HashSet;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.Map;
import java.util.Set;
import java.util.WeakHashMap;

/**
 * Initial defaults for the reviewed, unmodified QFun 1.3.4 APK.
 *
 * QQCurrentEnv.getCurrentUin() is upstream's common boundary before both
 * account SharedPreferences and ObjectStore paths are read. Its cached
 * "global"/last-account fallback is deliberately NOT sufficient to seed.
 * Module APK hashes must be checked by the build, not guessed here.
 */
public final class QFunAdapter {
    private static final String TAG = "MQGA-Preset";
    private static final Map<ClassLoader, State> INSTALLED = new WeakHashMap<>();

    private QFunAdapter() {}

    public static boolean install(Context context, ClassLoader moduleLoader,
                               JSONArray writes, String profileId) {
        synchronized (INSTALLED) {
            if (INSTALLED.containsKey(moduleLoader)) return true;
            try {
                State state = new State(context, moduleLoader, writes, profileId);
                if (state.switches.isEmpty() && state.objects.isEmpty()) return true;
                state.install();
                INSTALLED.put(moduleLoader, state);
                Log.i(TAG, "QFun account-bound defaults armed");
                return true;
            } catch (Throwable error) {
                Log.e(TAG, "QFun adapter could not be installed; no defaults applied", error);
                return false;
            }
        }
    }

    private static final class State {
        final Context context;
        final String profileId;
        final ClassLoader moduleLoader;
        Class<?> environmentClass;
        Method runtimeGetter;
        Method initData;
        boolean accountHooksInstalled;
        final Map<String, Boolean> switches = new LinkedHashMap<>();
        final Map<String, JSONObject> objects = new LinkedHashMap<>();
        final Set<Object> initializedObjects =
                Collections.newSetFromMap(new WeakHashMap<Object, Boolean>());
        final Set<String> completedScopes = new HashSet<>();
        final ThreadLocal<Boolean> inside = new ThreadLocal<>();
        long lastFailure;

        State(Context context, ClassLoader loader, JSONArray writes, String profileId)
                throws Exception {
            this.context = context;
            this.profileId = profileId;
            this.moduleLoader = loader;
            if (profileId == null || profileId.isEmpty()) throw new IOException("Missing profile identity");
            for (int index = 0; index < writes.length(); index++) {
                JSONObject write = writes.getJSONObject(index);
                if (!"QFun".equals(write.optString("module"))) continue;
                JSONObject storage = write.getJSONObject("storage");
                String key = storage.getString("key");
                Object value = write.get("proposed_value");
                String adapter = storage.getString("adapter");
                if ("shared_preferences".equals(adapter)) {
                    if (!"Boolean".equals(storage.getString("type")) || !(value instanceof Boolean)
                            || !safeName(key) || switches.containsKey(key)) {
                        throw new IOException("Invalid or duplicate QFun switch specification");
                    }
                    switches.put(key, (Boolean) value);
                } else if ("json_object".equals(adapter)) {
                    String[] parts = write.getString("item_id").split(":");
                    if (parts.length < 3 || !"QFun".equals(parts[0]) || !safeName(parts[1])
                            || configurationClass(parts[1]) == null) {
                        throw new IOException("Invalid QFun object specification");
                    }
                    JSONObject object = objects.get(parts[1]);
                    if (object == null) {
                        object = new JSONObject();
                        objects.put(parts[1], object);
                    }
                    if (object.has(key)) throw new IOException("Duplicate QFun JSON member");
                    object.put(key, typedJson(storage.getString("type"), value));
                } else {
                    throw new IOException("Unsupported QFun storage adapter");
                }
            }
            if (Boolean.TRUE.equals(switches.get("AutoGrabHb"))) {
                JSONObject packet = objects.get("AutoGrabHb");
                String[] required = {"delay", "minAverage", "keywords", "autoReply", "whiteList", "targetTypes"};
                if (packet == null) throw new IOException("Automatic packets require complete parameters");
                for (String key : required) {
                    if (!packet.has(key)) throw new IOException("Missing automatic packet parameter");
                }
            }
        }

        void install() throws Throwable {
            // The initial Xposed module loader cannot resolve QQ return types.
            // QFun connects its HybridClassLoader to the host/Tinker loader here,
            // before HostInfo.init and before any MainHook/ObjectStore loading.
            Class<?> module = Class.forName("me.yxp.qfun.common.ModuleLoader", false, moduleLoader);
            Method inject = module.getDeclaredMethod("injectClassLoader", ClassLoader.class);
            XC_MethodHook.Unhook injectionHook = null;
            XC_MethodHook.Unhook hostHook = null;
            try {
                injectionHook = XposedBridge.hookMethod(inject, new XC_MethodHook() {
                    @Override protected void afterHookedMethod(MethodHookParam param) {
                        if (!param.hasThrowable()) armAccountHooks();
                    }
                });
                if (injectionHook == null) throw new IOException("QFun loader hook was rejected");
                // Context initialization is a second exact upstream boundary, still
                // before MainHook.loadHook, if the first reflection attempt was early.
                Class<?> host = Class.forName("me.yxp.qfun.utils.qq.HostInfo", false, moduleLoader);
                Method initializeHost = host.getDeclaredMethod("init", Context.class);
                hostHook = XposedBridge.hookMethod(initializeHost, new XC_MethodHook() {
                    @Override protected void afterHookedMethod(MethodHookParam param) {
                        if (!param.hasThrowable()) armAccountHooks();
                    }
                });
                if (hostHook == null) throw new IOException("QFun host hook was rejected");
            } catch (Throwable error) {
                unhook(hostHook);
                unhook(injectionHook);
                throw error;
            }
        }

        synchronized void armAccountHooks() {
            if (accountHooksInstalled) return;
            try {
                installAccountHooks();
                accountHooksInstalled = true;
                Log.i(TAG, "QFun account hooks installed after host class-loader wiring");
            } catch (Throwable error) {
                Log.w(TAG, "QFun account hooks await host initialization", error);
            }
        }

        void installAccountHooks() throws Throwable {
            environmentClass = Class.forName("me.yxp.qfun.utils.qq.QQCurrentEnv", false, moduleLoader);
            runtimeGetter = environmentClass.getDeclaredMethod("getQQAppInterface");
            runtimeGetter.setAccessible(true);
            Class<?> clickable = Class.forName("me.yxp.qfun.hook.base.BaseClickableHookItem", false, moduleLoader);
            initData = clickable.getDeclaredMethod("initData");
            initData.setAccessible(true);
            Method currentUin = environmentClass.getDeclaredMethod("getCurrentUin");
            currentUin.setAccessible(true);
            // Observe objects already initialized against an unknown/old account.
            // Such objects must be reloaded before an account switch is enabled.
            XC_MethodHook.Unhook dataHook = null;
            XC_MethodHook.Unhook accountHook = null;
            try {
                dataHook = XposedBridge.hookMethod(initData, new XC_MethodHook() {
                    @Override protected void afterHookedMethod(MethodHookParam param) {
                        if (!param.hasThrowable()) {
                            synchronized (initializedObjects) {
                                initializedObjects.add(param.thisObject);
                            }
                        }
                    }
                });
                if (dataHook == null) throw new IOException("QFun configuration observer hook was rejected");
                accountHook = XposedBridge.hookMethod(currentUin, new XC_MethodHook(Integer.MAX_VALUE) {
                    @Override protected void afterHookedMethod(MethodHookParam param) {
                        if (!param.hasThrowable() && param.getResult() instanceof String) {
                            onAccount((String) param.getResult());
                        }
                    }
                });
                if (accountHook == null) throw new IOException("QFun account hook was rejected");
            } catch (Throwable error) {
                unhook(accountHook);
                unhook(dataHook);
                throw error;
            }
        }

        void onAccount(String observed) {
            if (Boolean.TRUE.equals(inside.get()) || !validUin(observed)) return;
            inside.set(Boolean.TRUE);
            try {
                synchronized (this) {
                    if (completedScopes.contains(observed)) return;
                    if (lastFailure != 0 && SystemClock.uptimeMillis() - lastFailure < 1000) return;
                    String actual = actualRuntimeUin();
                    if (!observed.equals(actual)) return;
                    seed(actual);
                }
            } catch (Throwable error) {
                synchronized (this) { lastFailure = SystemClock.uptimeMillis(); }
                Log.w(TAG, "QFun defaults deferred; no completion marker written", error);
            } finally {
                inside.remove();
            }
        }

        String actualRuntimeUin() throws Exception {
            Object environment = environmentClass.getDeclaredField("INSTANCE").get(null);
            Object runtime = runtimeGetter.invoke(environment);
            if (runtime == null) return null;
            // Some supported runtimes expose isLogin(); do not seed a known
            // logged-out session. Never fall back to QFun's cached currentUin.
            try {
                Method loggedIn = runtime.getClass().getMethod("isLogin");
                if (loggedIn.getReturnType() == boolean.class
                        && !Boolean.TRUE.equals(loggedIn.invoke(runtime))) return null;
            } catch (NoSuchMethodException ignored) {
                // The direct runtime UIN remains the authoritative boundary.
            }
            Method getter = runtime.getClass().getMethod("getCurrentUin");
            getter.setAccessible(true);
            Object value = getter.invoke(runtime);
            return value instanceof String && validUin((String) value) ? (String) value : null;
        }

        void seed(String uin) throws Exception {
            File markerRoot = new File(context.getFilesDir(), "mqga-presets/QFun");
            mkdirs(markerRoot);
            try (RandomAccessFile handle = new RandomAccessFile(new File(markerRoot, "lock"), "rw");
                 FileChannel channel = handle.getChannel(); FileLock lock = channel.lock()) {
                File done = new File(markerRoot, uin + ".done");
                File skipped = new File(markerRoot, uin + ".skipped");
                File pending = new File(markerRoot, uin + ".pending");
                if (done.isFile()) {
                    // Another process may have seeded after this process cached
                    // global defaults. Reload the real files, never reapply values.
                    hydrate(uin);
                    completedScopes.add(uin);
                    return;
                }
                if (skipped.isFile()) {
                    completedScopes.add(uin);
                    return;
                }
                File external = context.getExternalFilesDir(null);
                File parent = external == null ? null : external.getParentFile();
                if (parent == null) throw new IOException("Host external storage is not ready");
                File data = new File(parent, "QFun/" + uin + "/data");
                // MODE_MULTI_PROCESS matches upstream's mode and asks Android to
                // recheck disk when another QQ process initialized this scope.
                SharedPreferences prefs = context.getSharedPreferences("QFun_Config_" + uin, 4);
                boolean recovering = pending.isFile();
                if (recovering && !profileId.equals(readObject(pending).optString("profileId"))) {
                    mark(skipped, "different pending profile");
                    completedScopes.add(uin);
                    return;
                }
                if (!compatiblePreferences(prefs.getAll(), recovering)
                        || !compatibleObjects(data, recovering)) {
                    mark(skipped, "existing account configuration");
                    completedScopes.add(uin);
                    Log.i(TAG, "QFun existing account configuration preserved");
                    return;
                }
                if (!recovering) mark(pending, "parameters before switches");
                mkdirs(data);
                for (Map.Entry<String, JSONObject> entry : objects.entrySet()) {
                    File file = new File(data, entry.getKey());
                    if (!file.isFile() || file.length() == 0) writeAtomic(file, entry.getValue().toString());
                    if (!jsonEqual(readObject(file), entry.getValue())) {
                        throw new IOException("QFun parameter readback failed");
                    }
                }
                hydrate(uin);
                SharedPreferences.Editor edit = prefs.edit();
                for (Map.Entry<String, Boolean> entry : switches.entrySet()) {
                    if (!prefs.contains(entry.getKey())) edit.putBoolean(entry.getKey(), entry.getValue());
                }
                if (!edit.commit()) throw new IOException("QFun switch commit failed");
                for (Map.Entry<String, Boolean> entry : switches.entrySet()) {
                    if (!entry.getValue().equals(prefs.getAll().get(entry.getKey()))) {
                        throw new IOException("QFun switch readback failed");
                    }
                }
                mark(done, "applied");
                pending.delete();
                completedScopes.add(uin);
                lastFailure = 0;
                Log.i(TAG, "QFun defaults initialized for a new account scope");
            }
        }

        boolean compatiblePreferences(Map<String, ?> existing, boolean recovering) {
            if (!recovering) return existing.isEmpty();
            for (Map.Entry<String, ?> entry : existing.entrySet()) {
                if (!switches.containsKey(entry.getKey())
                        || !switches.get(entry.getKey()).equals(entry.getValue())) return false;
            }
            return true;
        }

        boolean compatibleObjects(File data, boolean recovering) throws Exception {
            if (!data.exists()) return true;
            if (!data.isDirectory()) return false;
            File[] files = data.listFiles();
            if (files == null) throw new IOException("Cannot inspect QFun account settings");
            for (File file : files) {
                if (file.isDirectory()) {
                    if (hasContent(file)) return false;
                    continue;
                }
                if (file.length() == 0) continue;
                String name = file.getName();
                if (recovering && (name.endsWith(".new") || name.endsWith(".bak"))) {
                    String base = name.substring(0, name.length() - 4);
                    if (objects.containsKey(base)) continue; // AtomicFile recovery artifact.
                }
                if (!recovering || !objects.containsKey(name)
                        || !jsonEqual(readObject(file), objects.get(name))) return false;
            }
            return true;
        }

        void hydrate(String uin) throws Exception {
            if (!uin.equals(actualRuntimeUin())) throw new IOException("Account changed before configuration reload");
            Set<Object> snapshot;
            synchronized (initializedObjects) { snapshot = new LinkedHashSet<>(initializedObjects); }
            // Upstream registers message listeners before initSwitchHookItem().
            // Include each configured singleton even if its first initData has
            // not happened; otherwise a newly enabled AutoGrabHb could consume
            // its constructor defaults in that startup window.
            for (String feature : objects.keySet()) {
                Class<?> type = Class.forName(configurationClass(feature), true, moduleLoader);
                Object singleton = type.getDeclaredField("INSTANCE").get(null);
                if (!initData.getDeclaringClass().isInstance(singleton)) {
                    throw new IOException("Unexpected QFun configurable singleton type");
                }
                snapshot.add(singleton);
            }
            for (Object object : snapshot) initData.invoke(object);
            if (!uin.equals(actualRuntimeUin())) throw new IOException("Account changed during configuration reload");
        }

        void mark(File file, String state) throws Exception {
            JSONObject marker = new JSONObject();
            marker.put("profileId", profileId);
            marker.put("state", state);
            marker.put("time", System.currentTimeMillis());
            writeAtomic(file, marker.toString());
        }
    }

    private static boolean validUin(String value) {
        return value != null && value.matches("[1-9][0-9]{4,19}");
    }

    private static void unhook(XC_MethodHook.Unhook hook) {
        if (hook == null) return;
        try { hook.unhook(); }
        catch (Throwable error) { Log.w(TAG, "Could not roll back a QFun adapter hook", error); }
    }

    private static boolean safeName(String value) {
        return value != null && value.matches("[A-Za-z][A-Za-z0-9_]*");
    }

    private static String configurationClass(String feature) {
        switch (feature) {
            case "RepeatMsg": return "me.yxp.qfun.hook.chat.RepeatMsg";
            case "ShowMsgTime": return "me.yxp.qfun.hook.chat.ShowMsgTime";
            case "CustomDevice": return "me.yxp.qfun.hook.device.CustomDevice";
            case "ModifyPicSummary": return "me.yxp.qfun.hook.msg.ModifyPicSummary";
            case "ShortcutBarCustom": return "me.yxp.qfun.hook.purify.ShortcutBarCustom";
            case "AutoKeepSpark": return "me.yxp.qfun.hook.social.AutoKeepSpark";
            case "AntiAtAll": return "me.yxp.qfun.hook.troop.AntiAtAll";
            case "AutoGroupClockIn": return "me.yxp.qfun.hook.troop.AutoGroupClockIn";
            case "AutoGrabHb": return "me.yxp.qfun.hook.redpacket.AutoGrabHb";
            default: return null;
        }
    }

    private static Object typedJson(String type, Object value) throws Exception {
        if ("Boolean".equals(type) && value instanceof Boolean) return value;
        if ("String".equals(type) && value instanceof String) return value;
        if (("Int".equals(type) || "Long".equals(type)) && value instanceof Number) {
            double number = ((Number) value).doubleValue();
            if (!Double.isNaN(number) && !Double.isInfinite(number) && number == Math.rint(number)
                    && (!"Int".equals(type) || (number >= Integer.MIN_VALUE && number <= Integer.MAX_VALUE))) {
                return "Int".equals(type) ? Integer.valueOf(((Number) value).intValue())
                        : Long.valueOf(((Number) value).longValue());
            }
        }
        if (("Float".equals(type) || "Double".equals(type)) && value instanceof Number) return value;
        if (("Set<String>".equals(type) || "List<String>".equals(type) || "Set<Int>".equals(type))
                && value instanceof JSONArray) {
            JSONArray values = (JSONArray) value;
            JSONArray result = new JSONArray();
            for (int index = 0; index < values.length(); index++) {
                result.put(typedJson("Set<Int>".equals(type) ? "Int" : "String", values.get(index)));
            }
            return result;
        }
        throw new IOException("Invalid typed QFun parameter");
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
        if (files == null) throw new IOException("Cannot inspect QFun configuration directory");
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
                if (bytes.size() > 1048576) throw new IOException("Oversized QFun configuration");
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
