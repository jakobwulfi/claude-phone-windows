// claude-phone's Visual Studio side: a toolbar button that opens a list of the Claude Code sessions the daemon knows
// about, where each runs (Visual Studio's Terminal, PowerShell, VS Code...) and the phone's state. It only displays: the
// daemon types phone messages into a Visual Studio terminal itself, through the console (claude_phone/consoleinject.py).
namespace ClaudePhone.VisualStudio;

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
    };

    /// <inheritdoc/>
    protected override void InitializeServices(IServiceCollection serviceCollection)
    {
        base.InitializeServices(serviceCollection);
    }
}
