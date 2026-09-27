using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;

internal static class OifShell {
    static readonly Guid AppModel = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");
    [StructLayout(LayoutKind.Sequential, Pack = 4)] struct PropertyKey { public Guid format; public uint id; public PropertyKey(uint id) { format = AppModel; this.id = id; } }
    [StructLayout(LayoutKind.Explicit, Size = 24)] struct Value {
        [FieldOffset(0)] public ushort type;
        [FieldOffset(8)] public IntPtr pointer;
    }
    [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IPropertyStore {
        uint GetCount(); void GetAt(uint index, out PropertyKey key); void GetValue(ref PropertyKey key, out Value value);
        void SetValue(ref PropertyKey key, ref Value value); void Commit();
    }
    [DllImport("shell32.dll", PreserveSig = false)] static extern void SHGetPropertyStoreForWindow(IntPtr hwnd, ref Guid iid, [MarshalAs(UnmanagedType.Interface)] out IPropertyStore store);
    static void Set(IPropertyStore store, uint id, string text) {
        var key = new PropertyKey(id); var value = new Value { type = 31, pointer = Marshal.StringToCoTaskMemUni(text) };
        try { store.SetValue(ref key, ref value); } finally { Marshal.FreeCoTaskMem(value.pointer); }
    }
    internal static void SetWindowIdentity(IntPtr hwnd, string appId, string executable) {
        IPropertyStore store; var iid = typeof(IPropertyStore).GUID;
        SHGetPropertyStoreForWindow(hwnd, ref iid, out store);
        try {
            Set(store, 5, appId);
            Set(store, 2, "\"" + executable + "\"");
            Set(store, 3, executable + ",0");
            Set(store, 4, "OIF");
        } finally { Marshal.ReleaseComObject(store); }
    }
    internal static void SetShortcutIdentity(string path, string appId) {
        object link = Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("00021401-0000-0000-C000-000000000046")));
        try {
            ((IPersistFile)link).Load(path, 2);
            var store = (IPropertyStore)link;
            Set(store, 5, appId); store.Commit();
            ((IPersistFile)link).Save(path, true);
        } finally { Marshal.ReleaseComObject(link); }
    }
}
