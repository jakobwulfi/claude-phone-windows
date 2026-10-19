using System.Net;
using Xunit;

namespace ClaudePhone.Tests;

public sealed class DaemonTests : IDisposable
{
    private readonly string home = Directory.CreateTempSubdirectory("cphone-vs-test-").FullName;

    public void Dispose() => Directory.Delete(home, recursive: true);

    private void Config(string text) => File.WriteAllText(Path.Combine(home, "config.toml"), text);

    [Fact]
    public void Port_is_read_from_the_config()
    {
        Config("ring_ext = \"201\"\ndaemon_port = 9001  # the daemon\n");

        Assert.Equal(9001, Daemon.Port(home));
    }

    [Theory]
    [InlineData("# daemon_port = 9001\n")]
    [InlineData("daemon_port = 0\n")]
    [InlineData("daemon_port = 70000\n")]
    [InlineData("daemon_port = \"x\"\n")]
    [InlineData("whisper_port = 9001\n")]
    [InlineData("")]
    public void Port_is_the_default_when_the_config_says_nothing_usable(string text)
    {
        Config(text);

        Assert.Equal(Daemon.DefaultPort, Daemon.Port(home));
    }

    [Fact]
    public void Port_is_the_default_without_a_config_file()
    {
        Assert.Equal(Daemon.DefaultPort, Daemon.Port(Path.Combine(home, "nowhere")));
    }

    /// <summary>Answers like a real handler would: it gives up when its token is cancelled.</summary>
    private sealed class StubHandler(Func<CancellationToken, Task<HttpResponseMessage>> reply) : HttpMessageHandler
    {
        public List<Uri> Requested { get; } = [];

        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
        {
            cancellationToken.ThrowIfCancellationRequested();
            Requested.Add(request.RequestUri!);
            return reply(cancellationToken);
        }
    }

    private static HttpResponseMessage Json(HttpStatusCode status, string body) =>
        new(status) { Content = new StringContent(body) };

    [Fact]
    public async Task Fetch_asks_the_daemon_on_localhost_and_parses_the_list()
    {
        var handler = new StubHandler(_ => Task.FromResult(Json(HttpStatusCode.OK,
            """{"sessions": [{"ext": 301, "label": "a", "status": "idle", "term_id": null}], "health": {}}""")));

        var list = await Daemon.FetchAsync(new HttpClient(handler), 8765, CancellationToken.None);

        Assert.Equal(new Uri("http://127.0.0.1:8765/sessions"), Assert.Single(handler.Requested));
        Assert.Equal(301, Assert.Single(list!.Sessions).Ext);
    }

    [Fact]
    public async Task Fetch_is_null_when_the_daemon_refuses_or_answers_with_something_else()
    {
        var refusing = new StubHandler(_ => Task.FromResult(Json(HttpStatusCode.Forbidden, "{}")));
        var odd = new StubHandler(_ => Task.FromResult(Json(HttpStatusCode.OK, """{"sessions": "nope"}""")));

        Assert.Null(await Daemon.FetchAsync(new HttpClient(refusing), 1, CancellationToken.None));
        Assert.Null(await Daemon.FetchAsync(new HttpClient(odd), 1, CancellationToken.None));
    }

    [Fact]
    public async Task Fetch_is_null_when_nothing_is_listening()
    {
        var down = new StubHandler(_ => throw new HttpRequestException("connection refused"));

        Assert.Null(await Daemon.FetchAsync(new HttpClient(down), 1, CancellationToken.None));
    }

    [Fact]
    public async Task Fetch_gives_up_on_a_daemon_that_does_not_answer()
    {
        var hung = new StubHandler(async token =>
        {
            await Task.Delay(Timeout.Infinite, token);
            return Json(HttpStatusCode.OK, "{}");
        });
        var timedOut = Daemon.FetchAsync(new HttpClient(hung), 1, CancellationToken.None);

        Assert.Null(await timedOut.WaitAsync(TimeSpan.FromSeconds(10)));
    }

    [Fact]
    public async Task Fetch_lets_a_cancelled_refresh_cancel()
    {
        using var cancelled = new CancellationTokenSource();
        await cancelled.CancelAsync();
        var handler = new StubHandler(_ => Task.FromResult(Json(HttpStatusCode.OK, "{}")));

        await Assert.ThrowsAnyAsync<OperationCanceledException>(
            () => Daemon.FetchAsync(new HttpClient(handler), 1, cancelled.Token));
    }
}
