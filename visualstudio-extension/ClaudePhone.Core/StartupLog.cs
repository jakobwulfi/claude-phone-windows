// A few lines on what the extension did when Visual Studio started it, so "did it start at launch?" can be answered
// from a file: ~/.claude-phone/logs/visualstudio-extension.log. It is cleared when it passes 64 KB.
namespace ClaudePhone;

public static class StartupLog
{
    public const string FileName = "visualstudio-extension.log";
    public const long MaxBytes = 64 * 1024;

    /// <summary>Appends a timestamped line. A log must never break the extension, so a failure to write is dropped.</summary>
    public static void Write(string phoneHome, string message)
    {
        try
        {
            var dir = Path.Combine(phoneHome, "logs");
            Directory.CreateDirectory(dir);
            var file = Path.Combine(dir, FileName);
            if (File.Exists(file) && new FileInfo(file).Length > MaxBytes)
            {
                File.Delete(file);
            }

            File.AppendAllText(file, $"{DateTime.Now:yyyy-MM-dd HH:mm:ss} {message}{Environment.NewLine}");
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            // Nowhere to write: the extension carries on.
        }
    }
}
