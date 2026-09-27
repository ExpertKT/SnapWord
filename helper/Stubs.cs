using System;

namespace SnapWheel
{
    // Minimal stubs so that SnapWheel's OCR sources compile standalone.
    // Only the members those three files actually touch are declared.
    internal static class Err
    {
        public static void Log(string tag, Exception ex) { }
        public static void Note(string tag, string msg) { }
        public static void Notify(string message) { }
    }

    internal static class Lang
    {
        // SnapWheel writes both languages inline; the helper always uses the Chinese wording.
        public static string T(string cn, string en) { return cn; }
        public static string T(string cn) { return cn; }
    }
}
