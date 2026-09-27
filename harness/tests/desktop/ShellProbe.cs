using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Web.Script.Serialization;
class ShellProbe {
    [StructLayout(LayoutKind.Sequential, Pack=4)] struct Key { public Guid format; public uint id; }
    [StructLayout(LayoutKind.Explicit, Size=24)] struct Value { [FieldOffset(0)] public ushort type; [FieldOffset(8)] public IntPtr text; }
    [ComImport,Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)] interface Store {
        void GetCount(out uint count); void GetAt(uint index,out Key key); void GetValue(ref Key key,out Value value);
        void SetValue(ref Key key,ref Value value); void Commit();
    }
    [DllImport("shell32.dll",PreserveSig=false)] static extern void SHGetPropertyStoreForWindow(IntPtr window,ref Guid iid,[MarshalAs(UnmanagedType.Interface)] out Store store);
    [DllImport("ole32.dll")] static extern int PropVariantClear(ref Value value);
    static Dictionary<string,string> Read(Store store) {
        var result=new Dictionary<string,string>();
        foreach (uint id in new uint[]{2,3,4,5}) {
            var key=new Key{format=new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"),id=id};Value v;
            store.GetValue(ref key,out v);
            try { result[id.ToString()]=v.type==31?Marshal.PtrToStringUni(v.text):null; } finally { PropVariantClear(ref v); }
        }
        return result;
    }
    [STAThread] static int Main(string[] args) {
        Store store;var iid=typeof(Store).GUID;
        SHGetPropertyStoreForWindow(new IntPtr(Int64.Parse(args[0])),ref iid,out store);
        Dictionary<string,string> window;
        try { window=Read(store); } finally {Marshal.ReleaseComObject(store);}
        Dictionary<string,string> shortcut=null;
        if(args.Length>1) {
            object link=Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("00021401-0000-0000-C000-000000000046")));
            try {((IPersistFile)link).Load(args[1],0);shortcut=Read((Store)link);}finally{Marshal.ReleaseComObject(link);}
        }
        Console.WriteLine(new JavaScriptSerializer().Serialize(new{window=window,shortcut=shortcut}));
        return String.IsNullOrEmpty(window["5"]) || (shortcut!=null&&shortcut["5"]!=window["5"])?1:0;
    }
}
