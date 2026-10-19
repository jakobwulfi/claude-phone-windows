// autostart: when it is on in config.toml, an editor that opens while claude-phone is down runs `phone up`.
// phone.exe's path comes from phone.json, which `phone up` and `phone config set autostart on` leave behind.
// The same rules as vscode-extension/src/autostart.ts.
using System.Diagnostics;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace ClaudePhone;

public enum AutostartOutcome
{
    Off,
    Running,
    NoPhone,
    Started,
}

public static class Autostart
{
    private static readonly Regex AutostartLine = new(@"^\s*autostart\s*=\s*(true|false)\b");

    /// <summary>The top-level <c>autostart</c> in config.toml. `phone config` writes the plain keys before any [table].</summary>
    public static bool IsOn(string toml)
    {
        foreach (var line in toml.Split('\n'))
        {
            if (line.TrimStart().StartsWith('['))
            {
                break;
            }

            var match = AutostartLine.Match(line);
            if (match.Success)
            {
                return match.Groups[1].Value == "true";
            }
        }

        return false;
    }

    /// <summary>phone.exe from phone.json, or null when the file is missing or says nothing usable.</summary>
    public static string? PhoneExe(string json)
    {
        try
        {
            using var doc = JsonDocument.Parse(json);
            return doc.RootElement.ValueKind == JsonValueKind.Object
                   && doc.RootElement.TryGetProperty("exe", out var exe)
                   && exe.ValueKind == JsonValueKind.String
                   && exe.GetString() is { Length: > 0 } path
                ? path
                : null;
        }
        catch (Exception e) when (e is JsonException or InvalidOperationException)
        {
            return null;
        }
    }

    /// <summary>Starts claude-phone if autostart is on and the daemon didn't answer.</summary>
    public static async Task<AutostartOutcome> RunAsync(
        string phoneHome, bool daemonRunning, Func<string, Task>? start = null)
    {
        if (!IsOn(ReadOrEmpty(Path.Combine(phoneHome, "config.toml"))))
        {
            return AutostartOutcome.Off;
        }

        if (daemonRunning)
        {
            return AutostartOutcome.Running;
        }

        if (PhoneExe(ReadOrEmpty(Path.Combine(phoneHome, "phone.json"))) is not { } exe)
        {
            return AutostartOutcome.NoPhone;
        }

        await (start ?? StartPhoneAsync)(exe);
        return AutostartOutcome.Started;
    }

    /// <summary>Runs `phone up` without a window and doesn't wait: it starts the services and returns, and they outlive it.</summary>
    public static Task StartPhoneAsync(string exe)
    {
        using var process = Process.Start(new ProcessStartInfo(exe, "up") { UseShellExecute = false, CreateNoWindow = true });
        return Task.CompletedTask;
    }

    private static string ReadOrEmpty(string file)
    {
        try
        {
            return File.ReadAllText(file);
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            return string.Empty;
        }
    }
}
