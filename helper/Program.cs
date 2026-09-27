using System;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Text;
using SnapWheel;

// ocr-helper: thin CLI over SnapWheel's OCR layer (system WinRT + bundled PaddleOCR).
// Usage:
//   ocr-helper.exe --probe [--engine auto|system|native]
//   ocr-helper.exe <image-path> [--engine auto|system|native]
// Always writes one JSON object to stdout; exit code only mirrors ok/available.
static class Program
{
    static string Esc(string s)
    {
        if (s == null) return "";
        StringBuilder b = new StringBuilder(s.Length + 16);
        foreach (char c in s)
        {
            if (c == '"') b.Append("\\\"");
            else if (c == '\\') b.Append("\\\\");
            else if (c == '\n') b.Append("\\n");
            else if (c == '\r') b.Append("\\r");
            else if (c == '\t') b.Append("\\t");
            else if (c < 0x20) b.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
            else b.Append(c);
        }
        return b.ToString();
    }

    // PP-OCR's text detection gives up on "very wide and short" images: the same pixels
    // read a whole paragraph when fed as the full 2578x1458 window, but only 1~2 characters
    // when cropped to a 1690x46 strip (measured, see docs\OCR-NOTES.md section 8).
    // Padding the strip vertically back to a sane aspect ratio fixes it for ~0 cost.
    const double PadRatio = 6.0;

    static Bitmap PadForOcr(Bitmap src)
    {
        if (src.Width <= 0 || src.Height <= 0) return src;
        int want = (int)Math.Ceiling(src.Width / PadRatio);
        if (src.Height >= want) return src;
        int pad = Math.Max(8, (want - src.Height + 1) / 2);
        Color bg = src.GetPixel(0, 0);          // 框选出来的图，左上角就是背景
        Bitmap dst = new Bitmap(src.Width, src.Height + pad * 2, System.Drawing.Imaging.PixelFormat.Format32bppArgb);
        using (Graphics g = Graphics.FromImage(dst))
        {
            g.Clear(bg);
            g.DrawImageUnscaled(src, 0, pad);
        }
        return dst;
    }

    static int Main(string[] args)
    {
        try { Console.OutputEncoding = new UTF8Encoding(false); } catch { }

        string file = null;
        string engine = "auto";
        bool probe = false;
        for (int i = 0; i < args.Length; i++)
        {
            string a = args[i];
            if (a == "--engine" && i + 1 < args.Length) engine = args[++i];
            else if (a == "--probe") probe = true;
            else if (file == null) file = a;
        }

        Ocr.Engine = engine;

        if (probe)
        {
            bool ok = Ocr.Available;
            Console.Write("{\"ok\":" + (ok ? "true" : "false") +
                          ",\"engine\":\"" + Esc(engine) +
                          "\",\"language\":\"" + Esc(Ocr.Language) +
                          "\",\"why\":\"" + Esc(Ocr.Why) + "\"}");
            return ok ? 0 : 2;
        }

        if (string.IsNullOrEmpty(file) || !File.Exists(file))
        {
            Console.Write("{\"ok\":false,\"error\":\"no such image: " + Esc(file) + "\"}");
            return 1;
        }

        string text = null;
        string error = null;
        long ms = 0;
        int iw = 0, ih = 0, pw = 0, ph = 0;
        try
        {
            // Read into memory first: Bitmap(string) would hold a lock on the file
            // (same trap SnapWheel hit in ImageIO.LoadWic).
            byte[] raw = File.ReadAllBytes(file);
            using (MemoryStream ms2 = new MemoryStream(raw, false))
            using (Bitmap original = new Bitmap(ms2))
            {
                iw = original.Width; ih = original.Height;
                Bitmap feed = PadForOcr(original);
                pw = feed.Width; ph = feed.Height;
                Stopwatch sw = Stopwatch.StartNew();
                try { text = Ocr.Recognize(feed, out error); }
                finally { if (!ReferenceEquals(feed, original)) feed.Dispose(); }
                sw.Stop();
                ms = sw.ElapsedMilliseconds;
            }
        }
        catch (Exception ex)
        {
            error = "helper: " + ex.Message;
        }

        bool ok2 = text != null;
        Console.Write("{\"ok\":" + (ok2 ? "true" : "false") +
                      ",\"engine\":\"" + Esc(engine) +
                      "\",\"language\":\"" + Esc(Ocr.Language) +
                      "\",\"ms\":" + ms.ToString(CultureInfo.InvariantCulture) +
                      ",\"iw\":" + iw + ",\"ih\":" + ih +
                      ",\"pw\":" + pw + ",\"ph\":" + ph +
                      ",\"text\":\"" + Esc(text) +
                      "\",\"error\":\"" + Esc(error) + "\"}");
        return ok2 ? 0 : 3;
    }
}
