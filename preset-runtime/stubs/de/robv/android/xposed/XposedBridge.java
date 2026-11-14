package de.robv.android.xposed;

/** Compile-only API surface. Implemented by the original LSPatch loader. */
public final class XposedBridge {
    public static XC_MethodHook.Unhook hookMethod(java.lang.reflect.Member method, XC_MethodHook callback) { return null; }
    public static java.util.Set<XC_MethodHook.Unhook> hookAllMethods(Class<?> c, String name, XC_MethodHook callback) { return null; }
    public static void log(String text) {}
    public static void log(Throwable error) {}
}
