namespace ClaudePhone.VisualStudio;

using System.Threading;
using System.Threading.Tasks;
using Microsoft.VisualStudio.Extensibility;
using Microsoft.VisualStudio.Extensibility.Commands;

/// <summary>The phone button: on a toolbar, in the Tools menu and under View > Other Windows.</summary>
[VisualStudioContribution]
public class ShowPhoneWindowCommand : Command
{
    /// <summary>With no placement of its own the toolbar is listed under View > Toolbars, where it is switched on.</summary>
    [VisualStudioContribution]
    public static ToolbarConfiguration PhoneToolbar => new("%ClaudePhone.Toolbar.DisplayName%")
    {
        Children = [ToolbarChild.Command<ShowPhoneWindowCommand>()],
    };

    /// <inheritdoc />
    public override CommandConfiguration CommandConfiguration => new("%ClaudePhone.ShowPhoneWindow.DisplayName%")
    {
        Placements = [CommandPlacement.KnownPlacements.ToolsMenu, CommandPlacement.KnownPlacements.ViewOtherWindowsMenu],
        Icon = new(ImageMoniker.Custom("Phone"), IconSettings.IconAndText),
    };

    /// <inheritdoc />
    public override async Task ExecuteCommandAsync(IClientContext context, CancellationToken cancellationToken)
    {
        await this.Extensibility.Shell().ShowToolWindowAsync<PhoneToolWindow>(activate: true, cancellationToken);
    }
}
