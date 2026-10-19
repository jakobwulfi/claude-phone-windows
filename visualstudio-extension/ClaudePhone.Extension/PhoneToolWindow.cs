namespace ClaudePhone.VisualStudio;

using System.Threading;
using System.Threading.Tasks;
using Microsoft.VisualStudio.Extensibility;
using Microsoft.VisualStudio.Extensibility.ToolWindows;
using Microsoft.VisualStudio.RpcContracts.RemoteUI;

/// <summary>The session list. Floating: it needs no room in anyone's window layout, and can be docked by hand.</summary>
[VisualStudioContribution]
public class PhoneToolWindow : ToolWindow
{
    private PhoneToolWindowData? data;

    public PhoneToolWindow()
    {
        this.Title = "claude-phone";
    }

    /// <inheritdoc />
    public override ToolWindowConfiguration ToolWindowConfiguration => new()
    {
        Placement = ToolWindowPlacement.Floating,
    };

    /// <inheritdoc />
    public override Task InitializeAsync(CancellationToken cancellationToken)
    {
        this.data = new PhoneToolWindowData();
        return Task.CompletedTask;
    }

    /// <inheritdoc />
    public override Task<IRemoteUserControl> GetContentAsync(CancellationToken cancellationToken) =>
        Task.FromResult<IRemoteUserControl>(new PhoneToolWindowControl(this.data!));

    /// <inheritdoc />
    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            this.data?.Stop();
        }

        base.Dispose(disposing);
    }
}
