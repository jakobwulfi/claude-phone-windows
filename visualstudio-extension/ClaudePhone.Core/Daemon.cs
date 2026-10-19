// Finding and asking the claude-phone daemon: which port it listens on, and its session list.
using System.Text.RegularExpressions;

namespace ClaudePhone;

public static class Daemon
{
    public const int DefaultPort = 8765;
    public static readonly TimeSpan FetchTimeout = TimeSpan.FromSeconds(2);

    private static readonly Regex PortLine = new(@"^\s*daemon_port\s*=\s*(\d+)", RegexOptions.Multiline);

    /// <summary>The folder the daemon keeps its files in: CLAUDE_PHONE_HOME, else ~/.claude-phone.</summary>
    public static string PhoneHome() =>
        Environment.GetEnvironmentVariable("CLAUDE_PHONE_HOME") is { Length: > 0 } home
            ? home
            : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".claude-phone");

    /// <summary>daemon_port from config.toml in the phone home, else the default. A commented-out line doesn't count.</summary>
    public static int Port(string phoneHome)
    {
        try
        {
            var match = PortLine.Match(File.ReadAllText(Path.Combine(phoneHome, "config.toml")));
            if (match.Success && int.TryParse(match.Groups[1].Value, out var port) && port is > 0 and <= 65535)
            {
                return port;
            }
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            // No readable config: the daemon runs on its default port.
        }

        return DefaultPort;
    }

    /// <summary>The daemon's session list, or null when it isn't running or answers with something else.</summary>
    public static async Task<SessionList?> FetchAsync(HttpClient http, int port, CancellationToken cancellationToken)
    {
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        timeout.CancelAfter(FetchTimeout);
        try
        {
            using var response = await http.GetAsync(new Uri($"http://127.0.0.1:{port}/sessions"), timeout.Token);
            return response.IsSuccessStatusCode
                ? SessionView.Parse(await response.Content.ReadAsStringAsync(timeout.Token))
                : null;
        }
        catch (Exception e) when (e is HttpRequestException or OperationCanceledException && !cancellationToken.IsCancellationRequested)
        {
            return null;
        }
    }
}
