// The daemon's session list (GET /sessions on 127.0.0.1) as the Visual Studio extension sees it: parsed and checked,
// grouped by where each session runs, and worded for the tool window. The same rules as vscode-extension/src/sessions.ts.
using System.Text.Json;

namespace ClaudePhone;

public sealed record PhoneSession(int Ext, string Label, string Status, string? TermId, string? Host);

public sealed record PhoneHealth(bool Freeswitch, bool DeskOnline, string RingExt, string? IpWarning);

public sealed record SessionList(IReadOnlyList<PhoneSession> Sessions, PhoneHealth Health);

public sealed record SessionGroup(string Title, IReadOnlyList<PhoneSession> Sessions);

public static class SessionView
{
    public const string VisualStudio = "Visual Studio";
    private const string VsCodePrefix = "vscode:";

    /// <summary>The list the daemon sent, or null when it isn't the shape we know (an odd field would throw later).</summary>
    public static SessionList? Parse(string json)
    {
        try
        {
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;
            if (root.ValueKind != JsonValueKind.Object
                || !root.TryGetProperty("sessions", out var sessions) || sessions.ValueKind != JsonValueKind.Array
                || !root.TryGetProperty("health", out var health) || health.ValueKind != JsonValueKind.Object)
            {
                return null;
            }

            var parsed = new List<PhoneSession>();
            foreach (var element in sessions.EnumerateArray())
            {
                var session = ParseSession(element);
                if (session is null)
                {
                    return null;
                }

                parsed.Add(session);
            }

            return new SessionList(parsed, ParseHealth(health));
        }
        catch (Exception e) when (e is JsonException or InvalidOperationException)
        {
            return null; // not JSON, or a string that isn't valid text (a lone surrogate escape)
        }
    }

    /// <summary>Where a session runs. A daemon that predates the host field only knew VS Code terminals.</summary>
    public static string HostOf(PhoneSession session) =>
        !string.IsNullOrWhiteSpace(session.Host) ? session.Host.Trim()
        : session.TermId?.StartsWith(VsCodePrefix, StringComparison.Ordinal) == true ? "VS Code"
        : "Terminal";

    /// <summary>One group per host, Visual Studio's own first and the rest alphabetical, sessions in extension order.</summary>
    public static IReadOnlyList<SessionGroup> Group(SessionList list) =>
        list.Sessions
            .OrderBy(s => s.Ext)
            .GroupBy(HostOf)
            .OrderBy(g => g.Key == VisualStudio ? 0 : 1)
            .ThenBy(g => g.Key, StringComparer.OrdinalIgnoreCase)
            .Select(g => new SessionGroup(g.Key, g.ToList()))
            .ToList();

    /// <summary>One line on the phone system: FreeSWITCH, the phone that rings, and an address warning if any.</summary>
    public static string HealthLine(PhoneHealth health)
    {
        var parts = new List<string>
        {
            health.Freeswitch
                ? $"phone {health.RingExt} {(health.DeskOnline ? "registered" : "not registered")}"
                : "FreeSWITCH not connected",
        };
        if (!string.IsNullOrEmpty(health.IpWarning))
        {
            parts.Add("address changed, see phone status");
        }

        return string.Join(" · ", parts);
    }

    public static string Headline(SessionList? list) => list switch
    {
        null => "claude-phone isn't running",
        { Sessions.Count: 1 } => "1 session",
        _ => $"{list.Sessions.Count} sessions",
    };

    private static PhoneSession? ParseSession(JsonElement e)
    {
        if (e.ValueKind != JsonValueKind.Object
            || !e.TryGetProperty("ext", out var ext) || ext.ValueKind != JsonValueKind.Number
            || !ext.TryGetInt32(out var extValue)
            || !TryString(e, "label", out var label) || label is null
            || !TryString(e, "status", out var status) || status is null
            || !TryString(e, "term_id", out var termId)
            || !TryString(e, "host", out var host))
        {
            return null;
        }

        return new PhoneSession(extValue, label, status, termId, host);
    }

    private static PhoneHealth ParseHealth(JsonElement e) => new(
        Bool(e, "freeswitch"), Bool(e, "desk_online"),
        TryString(e, "ring_ext", out var ring) ? ring ?? string.Empty : string.Empty,
        TryString(e, "ip_warning", out var warning) ? warning : null);

    /// <summary>False when the property is present with a type other than string or null; absent and null give null.</summary>
    private static bool TryString(JsonElement e, string name, out string? value)
    {
        value = null;
        if (!e.TryGetProperty(name, out var p) || p.ValueKind == JsonValueKind.Null)
        {
            return true;
        }

        if (p.ValueKind != JsonValueKind.String)
        {
            return false;
        }

        value = p.GetString();
        return true;
    }

    private static bool Bool(JsonElement e, string name) =>
        e.TryGetProperty(name, out var p) && p.ValueKind == JsonValueKind.True;
}
