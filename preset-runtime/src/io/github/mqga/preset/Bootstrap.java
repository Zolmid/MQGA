package io.github.mqga.preset;

import android.content.Context;
import android.content.SharedPreferences;
import android.util.Log;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.File;
import java.io.FileOutputStream;
import java.io.RandomAccessFile;
import java.nio.channels.FileLock;
import java.nio.charset.StandardCharsets;
import java.util.HashSet;
import java.util.Set;

/** Called only by the preset edition's loader, before legacy or modern module initialization. */
public final class Bootstrap {
    private static final String TAG = "MQGA-Preset";
    private static volatile Context host;
    private static JSONObject profile;
    private static final Set<String> installed = new HashSet<>();

    private Bootstrap() {}

    public static synchronized void start(Context context) {
        if (host != null || context == null) return;
        try {
            JSONObject parsed = new JSONObject(PresetData.json());
            if (!parsed.getBoolean("approved") || !"com.tencent.mobileqq".equals(context.getPackageName())) return;
            profile = parsed;
            host = context;
            seedBattery(context, moduleWrites("TSBattery"), parsed.getString("profile_id"));
            Log.i(TAG, "Preset bootstrap armed: " + parsed.getString("profile_id"));
        } catch (Throwable error) {
            Log.e(TAG, "Could not arm preset initialization; existing data was not replaced", error);
        }
    }

    /** Invoked after the real module ClassLoader exists, before either API instantiates its entry. */
    public static synchronized void moduleLoaded(ClassLoader moduleLoader, String apkPath) {
        if (host == null || moduleLoader == null || apkPath == null) return;
        String module = moduleForPath(apkPath);
        if (module == null || installed.contains(module)) return;
        try {
            JSONArray writes = moduleWrites(module);
            String id = profile.getString("profile_id");
            boolean armed;
            switch (module) {
                case "QAuxiliary": armed = QaAdapter.install(host, moduleLoader, writes, id); break;
                case "QFun": armed = QFunAdapter.install(host, moduleLoader, writes, id); break;
                case "QWish": armed = QWishAdapter.install(host, moduleLoader, writes, id); break;
                case "XAutoDaily": armed = XadAdapter.install(host, moduleLoader, writes, id); break;
                default: return;
            }
            if (armed) {
                installed.add(module);
                Log.i(TAG, module + " preset entry armed; scope completion is recorded separately");
            } else {
                Log.w(TAG, module + " preset entry was not armed; consult adapter error above");
            }
        } catch (Throwable error) {
            Log.e(TAG, "Preset adapter unavailable for " + module + "; no compatibility claim", error);
        }
    }

    private static String moduleForPath(String path) {
        File parent = new File(path).getParentFile();
        if (parent == null) return null;
        switch (parent.getName()) {
            case "io.github.qauxv": return "QAuxiliary";
            case "me.yxp.qfun": return "QFun";
            case "top.qwish.xposed": return "QWish";
            case "me.teble.xposed.autodaily": return "XAutoDaily";
            default: return null;
        }
    }

    private static JSONArray moduleWrites(String name) throws Exception {
        JSONArray all = profile.getJSONArray("writes");
        JSONArray out = new JSONArray();
        for (int i = 0; i < all.length(); i++) {
            JSONObject record = all.getJSONObject(i);
            if (name.equals(record.getString("module"))) out.put(record);
        }
        return out;
    }

    private static void seedBattery(Context context, JSONArray writes, String profileId) throws Exception {
        File dir = new File(context.getFilesDir(), "mqga-presets/TSBattery");
        if (!dir.isDirectory() && !dir.mkdirs()) throw new IllegalStateException("Cannot create battery preset status directory");
        File done = new File(dir, "host.done");
        File skipped = new File(dir, "host.skipped");
        try (RandomAccessFile guard = new RandomAccessFile(new File(dir, "host.lock"), "rw");
             FileLock lock = guard.getChannel().lock()) {
            if (done.exists() || skipped.exists()) return;
            // Acquire this instance only under the cross-process lock, before TSBattery is loaded.
            SharedPreferences prefs = context.getSharedPreferences("tsbattery_config", Context.MODE_PRIVATE);
            if (!prefs.getAll().isEmpty()) {
                marker(skipped, "Existing battery preferences preserved");
                return;
            }
            SharedPreferences.Editor editor = prefs.edit();
            for (int i = 0; i < writes.length(); i++) {
                JSONObject row = writes.getJSONObject(i);
                JSONObject store = row.getJSONObject("storage");
                String key = store.getString("key");
                if (!"Boolean".equals(store.getString("type")) || !allowedBatteryKey(key))
                    throw new IllegalStateException("Unexpected battery setting " + key);
                editor.putBoolean(key, row.getBoolean("proposed_value"));
            }
            if (writes.length() != 4 || !editor.commit()) throw new IllegalStateException("Battery preset commit failed");
            for (int i = 0; i < writes.length(); i++) {
                JSONObject row = writes.getJSONObject(i);
                String key = row.getJSONObject("storage").getString("key");
                if (!prefs.contains(key) || prefs.getBoolean(key, !row.getBoolean("proposed_value")) != row.getBoolean("proposed_value"))
                    throw new IllegalStateException("Battery preset readback failed");
            }
            marker(done, profileId);
            Log.i(TAG, "TSBattery: confirmed preset committed before module initialization");
        }
    }

    private static boolean allowedBatteryKey(String key) {
        return "disable_all_hook".equals(key) || "enable_qq_tim_protect_mode".equals(key)
            || "enable_kill_qq_tim_core_service".equals(key) || "enable_kill_qq_tim_core_service_child".equals(key);
    }

    private static void marker(File file, String text) throws Exception {
        try (FileOutputStream out = new FileOutputStream(file)) {
            out.write(text.getBytes(StandardCharsets.UTF_8));
            out.getFD().sync();
        }
    }
}
