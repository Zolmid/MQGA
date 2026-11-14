package de.robv.android.xposed;

/** Compile-only API surface. Never included in the runtime DEX. */
public abstract class XC_MethodHook {
    public XC_MethodHook() {}
    public XC_MethodHook(int priority) {}
    protected void beforeHookedMethod(MethodHookParam param) throws Throwable {}
    protected void afterHookedMethod(MethodHookParam param) throws Throwable {}
    public final class Unhook { public void unhook() {} }
    public static class MethodHookParam {
        public java.lang.reflect.Member method;
        public Object thisObject;
        public Object[] args;
        public Object getResult() { return null; }
        public void setResult(Object result) {}
        public Throwable getThrowable() { return null; }
        public boolean hasThrowable() { return false; }
        public void setThrowable(Throwable throwable) {}
        public Object getObjectExtra(String key) { return null; }
        public void setObjectExtra(String key, Object value) {}
    }
}
