using System;
using System.Globalization;
using System.Text;
using System.Threading;

namespace ZoomSdkProbe
{
    internal static class Program
    {
        private const int DefaultBlockMs = 20;

        private static int Main(string[] args)
        {
            try
            {
                if (HasArg(args, "--help") || HasArg(args, "-h"))
                {
                    PrintHelp();
                    return 0;
                }

                if (HasArg(args, "--simulate"))
                {
                    RunSimulation();
                    return 0;
                }

                WriteStatus("real Zoom SDK mode requested");
                WriteError(
                    "Real Zoom SDK integration is not compiled yet. " +
                    "Install the Zoom Meeting SDK for Windows and wire raw audio callbacks here, " +
                    "or run this probe with --simulate to test the Python process adapter.");
                return 2;
            }
            catch (Exception ex)
            {
                WriteError(ex.GetType().Name + ": " + ex.Message);
                return 1;
            }
        }

        private static void RunSimulation()
        {
            int sampleRate = GetEnvInt("ZOOM_SDK_SAMPLE_RATE", 48000);
            int channels = GetEnvInt("ZOOM_SDK_CHANNELS", 1);
            double seconds = GetEnvDouble("ZOOM_SDK_PROBE_SECONDS", 3.0);
            string meetingNumber = Environment.GetEnvironmentVariable("ZOOM_SDK_MEETING_NUMBER") ?? "";
            string displayName = Environment.GetEnvironmentVariable("ZOOM_SDK_DISPLAY_NAME") ?? "ZoomSdkProbe";

            WriteStatus(
                "simulating raw audio; " +
                "meeting=" + Redact(meetingNumber) + ", " +
                "display_name=" + displayName + ", " +
                "sample_rate=" + sampleRate.ToString(CultureInfo.InvariantCulture) + ", " +
                "channels=" + channels.ToString(CultureInfo.InvariantCulture));

            int framesPerBlock = Math.Max(1, sampleRate * DefaultBlockMs / 1000);
            int totalBlocks = Math.Max(1, (int)Math.Round(seconds * 1000.0 / DefaultBlockMs));
            double amplitude = 0.2 * short.MaxValue;

            for (int block = 0; block < totalBlocks; block++)
            {
                byte[] pcm = new byte[framesPerBlock * channels * 2];
                for (int frame = 0; frame < framesPerBlock; frame++)
                {
                    double t = ((block * framesPerBlock) + frame) / (double)sampleRate;
                    short sample = (short)Math.Round(Math.Sin(2.0 * Math.PI * 440.0 * t) * amplitude);
                    for (int channel = 0; channel < channels; channel++)
                    {
                        int offset = ((frame * channels) + channel) * 2;
                        pcm[offset] = (byte)(sample & 0xff);
                        pcm[offset + 1] = (byte)((sample >> 8) & 0xff);
                    }
                }
                WriteAudio(sampleRate, channels, pcm);
                Thread.Sleep(1);
            }

            WriteDone();
        }

        private static bool HasArg(string[] args, string value)
        {
            foreach (string arg in args)
            {
                if (string.Equals(arg, value, StringComparison.OrdinalIgnoreCase))
                {
                    return true;
                }
            }
            return false;
        }

        private static int GetEnvInt(string name, int defaultValue)
        {
            string value = Environment.GetEnvironmentVariable(name);
            int parsed;
            return int.TryParse(value, NumberStyles.Integer, CultureInfo.InvariantCulture, out parsed)
                ? parsed
                : defaultValue;
        }

        private static double GetEnvDouble(string name, double defaultValue)
        {
            string value = Environment.GetEnvironmentVariable(name);
            double parsed;
            return double.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture, out parsed)
                ? parsed
                : defaultValue;
        }

        private static string Redact(string value)
        {
            if (string.IsNullOrEmpty(value))
            {
                return "";
            }
            if (value.Length <= 4)
            {
                return "****";
            }
            return "****" + value.Substring(value.Length - 4);
        }

        private static void PrintHelp()
        {
            Console.Error.WriteLine("ZoomSdkProbe");
            Console.Error.WriteLine("  --simulate  Emit generated PCM using the Python process-adapter JSON protocol.");
        }

        private static void WriteStatus(string message)
        {
            WriteJson("{\"type\":\"status\",\"message\":\"" + JsonEscape(message) + "\"}");
        }

        private static void WriteError(string message)
        {
            WriteJson("{\"type\":\"error\",\"message\":\"" + JsonEscape(message) + "\"}");
        }

        private static void WriteDone()
        {
            WriteJson("{\"type\":\"done\"}");
        }

        private static void WriteAudio(int sampleRate, int channels, byte[] pcm)
        {
            WriteJson(
                "{\"type\":\"audio\"," +
                "\"sample_rate\":" + sampleRate.ToString(CultureInfo.InvariantCulture) + "," +
                "\"channels\":" + channels.ToString(CultureInfo.InvariantCulture) + "," +
                "\"pcm_s16le_base64\":\"" + Convert.ToBase64String(pcm) + "\"}");
        }

        private static void WriteJson(string json)
        {
            Console.Out.WriteLine(json);
            Console.Out.Flush();
        }

        private static string JsonEscape(string value)
        {
            if (value == null)
            {
                return "";
            }

            StringBuilder builder = new StringBuilder(value.Length + 8);
            foreach (char ch in value)
            {
                switch (ch)
                {
                    case '\\':
                        builder.Append("\\\\");
                        break;
                    case '"':
                        builder.Append("\\\"");
                        break;
                    case '\n':
                        builder.Append("\\n");
                        break;
                    case '\r':
                        builder.Append("\\r");
                        break;
                    case '\t':
                        builder.Append("\\t");
                        break;
                    default:
                        if (ch < ' ')
                        {
                            builder.Append("\\u");
                            builder.Append(((int)ch).ToString("x4", CultureInfo.InvariantCulture));
                        }
                        else
                        {
                            builder.Append(ch);
                        }
                        break;
                }
            }
            return builder.ToString();
        }
    }
}
