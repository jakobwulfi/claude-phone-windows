namespace ClaudePhone.VisualStudio;

using System.Threading;
using System.Threading.Tasks;
using Microsoft.VisualStudio.Extensibility.UI;

/// <summary>The remote control behind PhoneToolWindowControl.xaml (matched by name).</summary>
internal class PhoneToolWindowControl : RemoteUserControl
{
    private readonly PhoneToolWindowData data;

    public PhoneToolWindowControl(PhoneToolWindowData data)
        : base(dataContext: data)
    {
        this.data = data;
    }

    /// <inheritdoc />
    public override Task ControlLoadedAsync(CancellationToken cancellationToken)
    {
        this.data.Start();
        return base.ControlLoadedAsync(cancellationToken);
    }

    /// <inheritdoc />
    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            this.data.Stop();
        }

        base.Dispose(disposing);
    }
}
