using Xunit;

namespace ClaudePhone.Tests;

public sealed class AutostartTests : IDisposable
{
    private readonly string home = Directory.CreateTempSubdirectory("cphone-vs-autostart-").FullName;
    private readonly List<string> started = [];

    public void Dispose() => Directory.Delete(home, recursive: true);

    private Task Start(string exe)
    {
        started.Add(exe);
        return Task.CompletedTask;
    }

    private void Write(string name, string text) => File.WriteAllText(Path.Combine(home, name), text);

    [Theory]
    [InlineData("owner_name = \"Sam\"\r\nautostart = true            # start when an editor opens\r\n", true)]
    [InlineData("autostart = false\n", false)]
    [InlineData("owner_name = \"Sam\"\n", false)]
    [InlineData("[repo_voices.x]\nautostart = true\n", false)]
    [InlineData("# autostart = true\n", false)]
    [InlineData("autostart = \"yes\"\n", false)]
    [InlineData("", false)]
    public void IsOn_reads_the_top_level_setting_only(string toml, bool expected)
    {
        Assert.Equal(expected, Autostart.IsOn(toml));
    }

    [Theory]
    [InlineData("""{"exe": "C:\\venv\\Scripts\\phone.exe"}""", "C:\\venv\\Scripts\\phone.exe")]
    [InlineData("""{"exe": ""}""", null)]
    [InlineData("""{"exe": 5}""", null)]
    [InlineData("""{}""", null)]
    [InlineData("[]", null)]
    [InlineData("not json", null)]
    [InlineData("", null)]
    public void PhoneExe_is_the_path_in_phone_json_or_nothing(string json, string? expected)
    {
        Assert.Equal(expected, Autostart.PhoneExe(json));
    }

    [Fact]
    public async Task Runs_phone_up_only_when_autostart_is_on_and_the_daemon_is_down()
    {
        Write("config.toml", "autostart = true\n");
        Write("phone.json", """{"exe": "C:\\phone.exe"}""");

        Assert.Equal(AutostartOutcome.Running, await Autostart.RunAsync(home, daemonRunning: true, Start));
        Assert.Empty(started);
        Assert.Equal(AutostartOutcome.Started, await Autostart.RunAsync(home, daemonRunning: false, Start));
        Assert.Equal(["C:\\phone.exe"], started);
    }

    [Fact]
    public async Task Does_nothing_when_autostart_is_off_or_there_is_no_config()
    {
        Assert.Equal(AutostartOutcome.Off, await Autostart.RunAsync(home, false, Start));
        Write("config.toml", "autostart = false\n");
        Write("phone.json", """{"exe": "C:\\phone.exe"}""");

        Assert.Equal(AutostartOutcome.Off, await Autostart.RunAsync(home, false, Start));
        Assert.Empty(started);
    }

    [Fact]
    public async Task Says_so_when_it_does_not_know_where_phone_exe_is()
    {
        Write("config.toml", "autostart = true\n");

        Assert.Equal(AutostartOutcome.NoPhone, await Autostart.RunAsync(home, false, Start));
        Assert.Empty(started);
    }

    [Fact]
    public async Task Lets_a_failing_start_surface_to_the_caller()
    {
        Write("config.toml", "autostart = true\n");
        Write("phone.json", """{"exe": "C:\\phone.exe"}""");

        await Assert.ThrowsAsync<InvalidOperationException>(() => Autostart.RunAsync(
            home, false, _ => throw new InvalidOperationException("can't start")));
    }

    [Fact]
    public async Task Starts_a_real_program_without_waiting_and_reports_one_that_is_not_there()
    {
        // where.exe stands in for phone.exe: it takes the argument `up`, looks for a file of that name and exits.
        await Autostart.StartPhoneAsync(Path.Combine(Environment.SystemDirectory, "where.exe"));

        await Assert.ThrowsAnyAsync<Exception>(() => Autostart.StartPhoneAsync(Path.Combine(home, "no-such-phone.exe")));
    }

    [Fact]
    public void StartupLog_appends_timestamped_lines_under_logs()
    {
        StartupLog.Write(home, "autostart: started");
        StartupLog.Write(home, "autostart: running");

        var lines = File.ReadAllLines(Path.Combine(home, "logs", StartupLog.FileName));
        Assert.Equal(2, lines.Length);
        Assert.Matches(@"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d autostart: started$", lines[0]);
    }

    [Fact]
    public void StartupLog_starts_over_once_it_is_large()
    {
        var file = Path.Combine(home, "logs", StartupLog.FileName);
        Directory.CreateDirectory(Path.GetDirectoryName(file)!);
        File.WriteAllText(file, new string('x', (int)StartupLog.MaxBytes + 1));

        StartupLog.Write(home, "fresh");

        Assert.Single(File.ReadAllLines(file));
    }

    [Fact]
    public void StartupLog_never_throws_when_it_cannot_write()
    {
        var blocked = Path.Combine(home, "a-file");
        File.WriteAllText(blocked, "not a folder");

        StartupLog.Write(blocked, "nowhere to go");
    }
}
