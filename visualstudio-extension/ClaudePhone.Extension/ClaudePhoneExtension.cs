// claude-phone's Visual Studio side: a toolbar button that opens a list of the Claude Code sessions the daemon knows
// about, where each runs (Visual Studio's Terminal, PowerShell, VS Code...) and the phone's state. It only displays: the
// daemon types phone messages into a Visual Studio terminal itself, through the console (claude_phone/consoleinject.py).
namespace ClaudePhone.VisualStudio;

using System;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.VisualStudio.Extensibility;

[VisualStudioContribution]
public class ClaudePhoneExtension : Extension
{
    /// <inheritdoc/>
    public override ExtensionConfiguration ExtensionConfiguration => new()
    {
        Metadata = new(
            id: "ClaudePhone.6f1b6d0e-3c1e-4c53-9a4e-5b7a0f2d9c11",
            version: this.ExtensionAssemblyVersion,
            publisherName: "jakobwulfi",
            displayName: "claude-phone",
            description: "Lists the Claude Code sessions claude-phone knows about, and where each one runs."),

        // An extension is otherwise loaded when something in it is first used. Autostart has to run when Visual
        // Studio opens, so: loaded whether or not a solution is open, which is always.
        LoadedWhen = ActivationConstraint.SolutionState(SolutionState.NoSolution)
                     | ActivationConstraint.SolutionState(SolutionState.Exists),
    };

    /// <inheritdoc/>
    protected override void InitializeServices(IServiceCollection serviceCollection)
    {
        base.InitializeServices(serviceCollection);
    }

    /// <inheritdoc/>
    protected override async Task OnInitializedAsync(VisualStudioExtensibility extensibility, CancellationToken cancellationToken)
    {
        await base.OnInitializedAsync(extensibility, cancellationToken);
        await StartClaudePhoneIfWantedAsync(cancellationToken);
    }

    /// <summary>With autostart on, runs `phone up` when the daemon isn't answering. What happened goes in a log file.</summary>
    private static async Task StartClaudePhoneIfWantedAsync(CancellationToken cancellationToken)
    {
        var home = Daemon.PhoneHome();
        try
        {
            using var http = Daemon.NewHttpClient();
            var running = await Daemon.FetchAsync(http, Daemon.Port(home), cancellationToken) is not null;
            var outcome = await Autostart.RunAsync(home, running);
            StartupLog.Write(home, "extension loaded; autostart: " + outcome switch
            {
                AutostartOutcome.Off => "off",
                AutostartOutcome.Running => "on, claude-phone already running",
                AutostartOutcome.Started => "on, ran `phone up`",
                _ => "on, but phone.exe isn't known yet: run `phone up` once",
            });
        }
        catch (Exception e) when (e is not OperationCanceledException)
        {
            StartupLog.Write(home, $"extension loaded; autostart failed: {e.Message}");
        }
    }
}
