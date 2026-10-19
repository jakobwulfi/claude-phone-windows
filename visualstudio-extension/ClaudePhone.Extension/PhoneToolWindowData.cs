namespace ClaudePhone.VisualStudio;

using System;
using System.Collections.Generic;
using System.Linq;
using System.Net.Http;
using System.Runtime.Serialization;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.VisualStudio.Extensibility.UI;

/// <summary>One line of the list: a host heading, or a session (indented under its host) with its status.</summary>
[DataContract]
internal sealed class SessionRow
{
    public SessionRow(string title, string detail)
    {
        this.Title = title;
        this.Detail = detail;
    }

    [DataMember]
    public string Title { get; }

    [DataMember]
    public string Detail { get; }
}

/// <summary>What the tool window shows, refreshed from the daemon every 15 seconds while it is open.</summary>
[DataContract]
internal sealed class PhoneToolWindowData : NotifyPropertyChangedObject
{
    private const string Indent = "    ";

    // Localhost only: a proxy configured for the system must never get this request.
    private static readonly HttpClient Http = new(new SocketsHttpHandler { UseProxy = false });
    private static readonly TimeSpan RefreshInterval = TimeSpan.FromSeconds(15);

    private readonly object sync = new();
    private readonly SemaphoreSlim refreshing = new(1, 1); // the loop and the Refresh button must not interleave
    private CancellationTokenSource? loop;
    private IReadOnlyList<SessionRow> shown = [];
    private string headline = "claude-phone";
    private string health = string.Empty;

    public PhoneToolWindowData()
    {
        this.RefreshCommand = new AsyncCommand((parameter, cancellationToken) => this.RefreshAsync(cancellationToken));
    }

    [DataMember]
    public string Headline
    {
        get => this.headline;
        set => this.SetProperty(ref this.headline, value);
    }

    [DataMember]
    public string Health
    {
        get => this.health;
        set => this.SetProperty(ref this.health, value);
    }

    [DataMember]
    public ObservableList<SessionRow> Rows { get; } = new();

    [DataMember]
    public IAsyncCommand RefreshCommand { get; }

    /// <summary>Starts refreshing every 15 seconds, unless it already is. Can start again after <see cref="Stop"/>.</summary>
    public void Start()
    {
        lock (this.sync)
        {
            if (this.loop is null)
            {
                this.loop = new CancellationTokenSource();
                _ = this.RunAsync(this.loop.Token);
            }
        }
    }

    public void Stop()
    {
        lock (this.sync)
        {
            this.loop?.Cancel();
            this.loop?.Dispose();
            this.loop = null;
        }
    }

    private async Task RunAsync(CancellationToken cancellationToken)
    {
        try
        {
            while (true)
            {
                await this.RefreshAsync(cancellationToken);
                await Task.Delay(RefreshInterval, cancellationToken);
            }
        }
        catch (OperationCanceledException)
        {
            // The window was closed.
        }
    }

    private async Task RefreshAsync(CancellationToken cancellationToken)
    {
        await this.refreshing.WaitAsync(cancellationToken);
        try
        {
            this.Show(await Daemon.FetchAsync(Http, Daemon.Port(Daemon.PhoneHome()), cancellationToken));
        }
        catch (Exception e) when (e is not OperationCanceledException)
        {
            this.Headline = "claude-phone: couldn't refresh";
            this.Health = e.Message;
        }
        finally
        {
            this.refreshing.Release();
        }
    }

    private void Show(SessionList? list)
    {
        var rows = new List<SessionRow>();
        foreach (var group in list is null ? [] : SessionView.Group(list))
        {
            rows.Add(new SessionRow(group.Title, string.Empty));
            rows.AddRange(group.Sessions.Select(s => new SessionRow($"{Indent}{s.Ext}  {s.Label}", s.Status)));
        }

        this.Headline = SessionView.Headline(list);
        this.Health = list is null ? "Start it with /claude-phone in Claude Code, or `phone up`." : SessionView.HealthLine(list.Health);
        if (!rows.Select(r => (r.Title, r.Detail)).SequenceEqual(this.shown.Select(r => (r.Title, r.Detail))))
        {
            this.Rows.Clear(); // only when something changed: the list would blink and lose its scroll position every 15 s
            this.Rows.AddRange(rows);
            this.shown = rows;
        }
    }
}
