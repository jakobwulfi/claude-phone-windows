using Xunit;

namespace ClaudePhone.Tests;

public class SessionViewTests
{
    private const string Health = """{"freeswitch": true, "desk_online": true, "ring_ext": "201", "ip_warning": null}""";

    private static string Json(string sessions, string health = Health) =>
        $$"""{"sessions": [{{sessions}}], "health": {{health}}}""";

    private static PhoneSession Session(int ext, string? host, string? termId = null) =>
        new(ext, $"repo/{ext}", "idle", termId, host);

    private static SessionList ListOf(params PhoneSession[] sessions) =>
        new(sessions, new PhoneHealth(true, true, "201", null));

    [Fact]
    public void Parse_reads_what_the_daemon_sends()
    {
        var list = SessionView.Parse(Json(
            """{"ext": 301, "label": "site/main", "status": "working", "term_id": "con:10:1", "host": "Visual Studio"}"""));

        Assert.NotNull(list);
        var session = Assert.Single(list.Sessions);
        Assert.Equal(new PhoneSession(301, "site/main", "working", "con:10:1", "Visual Studio"), session);
        Assert.Equal(new PhoneHealth(true, true, "201", null), list.Health);
    }

    [Fact]
    public void Parse_accepts_a_daemon_that_sends_no_host_or_a_null_one()
    {
        var list = SessionView.Parse(Json(
            """{"ext": 1, "label": "a", "status": "idle", "term_id": "vscode:1"}, {"ext": 2, "label": "b", "status": "idle", "term_id": null, "host": null}"""));

        Assert.Equal(2, list!.Sessions.Count);
        Assert.Null(list.Sessions[0].Host);
        Assert.Null(list.Sessions[1].TermId);
    }

    [Theory]
    [InlineData("""{"ext": 1, "label": "a", "status": "idle", "host": 7}""")]
    [InlineData("""{"ext": 1, "label": "a", "status": "idle", "term_id": 12}""")]
    [InlineData("""{"ext": 1, "label": "a", "status": "idle", "term_id": {}}""")]
    [InlineData("""{"ext": 1, "label": "a"}""")]
    [InlineData("""{"ext": "1", "label": "a", "status": "idle"}""")]
    [InlineData("""{"ext": 1.5, "label": "a", "status": "idle"}""")]
    [InlineData("""{"label": "a", "status": "idle"}""")]
    [InlineData("7")]
    public void Parse_refuses_the_whole_answer_when_a_session_has_a_field_of_the_wrong_type(string session)
    {
        Assert.Null(SessionView.Parse(Json(session)));
    }

    [Theory]
    [InlineData("")]
    [InlineData("not json")]
    [InlineData("[]")]
    [InlineData("""{"sessions": "nope", "health": {}}""")]
    [InlineData("""{"sessions": []}""")]
    [InlineData("""{"sessions": [], "health": 3}""")]
    public void Parse_refuses_an_answer_of_another_shape(string body)
    {
        Assert.Null(SessionView.Parse(body));
    }

    [Fact]
    public void Parse_refuses_a_label_that_is_not_valid_text()
    {
        // A lone surrogate escape parses as JSON but cannot be read back as a string.
        Assert.Null(SessionView.Parse(Json("""{"ext": 1, "label": "\ud800", "status": "idle"}""")));
    }

    [Fact]
    public void Parse_survives_an_odd_health()
    {
        var list = SessionView.Parse("""{"sessions": [], "health": {"freeswitch": "yes", "ring_ext": 5}}""");

        Assert.Equal(new PhoneHealth(false, false, string.Empty, null), list!.Health);
    }

    [Fact]
    public void HostOf_falls_back_to_where_the_terminal_id_says()
    {
        Assert.Equal("PowerShell", SessionView.HostOf(Session(1, " PowerShell ")));
        Assert.Equal("VS Code", SessionView.HostOf(Session(1, null, "vscode:5")));
        Assert.Equal("VS Code", SessionView.HostOf(Session(1, "  ", "vscode:5")));
        Assert.Equal("Terminal", SessionView.HostOf(Session(1, null, "con:5:6")));
        Assert.Equal("Terminal", SessionView.HostOf(Session(1, null)));
    }

    [Fact]
    public void Group_puts_visual_studio_first_then_the_other_hosts_alphabetically_with_sessions_in_extension_order()
    {
        var groups = SessionView.Group(ListOf(
            Session(305, "Windows Terminal"), Session(304, "PowerShell"), Session(303, "Visual Studio"),
            Session(302, "VS Code"), Session(301, "Visual Studio"), Session(306, "cmd shell")));

        Assert.Equal(
            new[] { "Visual Studio", "cmd shell", "PowerShell", "VS Code", "Windows Terminal" },
            groups.Select(g => g.Title));
        Assert.Equal(new[] { 301, 303 }, groups[0].Sessions.Select(s => s.Ext));
    }

    [Fact]
    public void Group_is_empty_without_sessions()
    {
        Assert.Empty(SessionView.Group(ListOf()));
    }

    [Fact]
    public void Group_puts_a_session_of_an_older_daemon_under_vs_code()
    {
        var groups = SessionView.Group(ListOf(Session(301, null, "vscode:1")));

        Assert.Equal("VS Code", Assert.Single(groups).Title);
    }

    [Theory]
    [InlineData(true, true, null, "phone 201 registered")]
    [InlineData(true, false, null, "phone 201 not registered")]
    [InlineData(false, true, null, "FreeSWITCH not connected")]
    [InlineData(true, true, "moved", "phone 201 registered · address changed, see phone status")]
    public void HealthLine_names_the_phone_and_whether_it_is_registered(
        bool freeswitch, bool desk, string? warning, string expected)
    {
        Assert.Equal(expected, SessionView.HealthLine(new PhoneHealth(freeswitch, desk, "201", warning)));
    }

    [Fact]
    public void Headline_counts_sessions_or_says_the_daemon_is_off()
    {
        Assert.Equal("claude-phone isn't running", SessionView.Headline(null));
        Assert.Equal("1 session", SessionView.Headline(ListOf(Session(1, "x"))));
        Assert.Equal("0 sessions", SessionView.Headline(ListOf()));
        Assert.Equal("2 sessions", SessionView.Headline(ListOf(Session(1, "x"), Session(2, "y"))));
    }
}
