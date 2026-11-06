# Third party notices

This application uses Windows Community Toolkit controls and adapts selected general settings patterns from Microsoft PowerToys. The attribution below distinguishes a distributed package dependency from source and design references. This notice does not change the licenses of those components or of this application.

## Microsoft PowerToys

- Project: [microsoft/PowerToys](https://github.com/microsoft/PowerToys).
- Reference revision: [`2d28a562bfb5f8a76c4a43bbd8c3939f827bb368`](https://github.com/microsoft/PowerToys/commit/2d28a562bfb5f8a76c4a43bbd8c3939f827bb368).
- Copyright: Microsoft Corporation. All rights reserved.
- License: MIT. The original upstream text is included in [LICENSES/PowerToys-MIT.txt](LICENSES/PowerToys-MIT.txt).
- Adaptation scope: settings cards and expanders; application and caption theme handling; early interface language selection; update process handoff and executable read locking; system tray recovery.

The application primarily adapts these patterns to its existing WinUI frontend and Python worker. It does not include the complete PowerToys runner, settings application, or updater. The mapping to upstream files and the differences in trust, startup, and installation behavior are recorded in [docs/POWERTOYS_ADAPTATION.md](docs/POWERTOYS_ADAPTATION.md).

The PowerToys license copy was retrieved from the reference revision's [LICENSE](https://github.com/microsoft/PowerToys/blob/2d28a562bfb5f8a76c4a43bbd8c3939f827bb368/LICENSE). Its Git blob SHA-1 is `b2d78c9e34231fea76d5eaf8a6d80dbb737b2948`.

## Windows Community Toolkit

- Package: `CommunityToolkit.WinUI.Controls.SettingsControls`.
- Referenced version: **8.2.251219**.
- Controls used: `SettingsCard` and `SettingsExpander`, in `CommunityToolkit.WinUI.Controls`.
- Package metadata and license declaration: [NuGet version 8.2.251219](https://www.nuget.org/packages/CommunityToolkit.WinUI.Controls.SettingsControls/8.2.251219).
- Copyright: .NET Foundation and Contributors. All rights reserved.
- License: MIT. The original upstream text is included in [LICENSES/CommunityToolkit-MIT.txt](LICENSES/CommunityToolkit-MIT.txt).

This is a package dependency with its own license; PowerToys' MIT license is not the license grant for Community Toolkit. The license copy comes from the official [CommunityToolkit/Windows License.md](https://github.com/CommunityToolkit/Windows/blob/a2fa47ad72cb95ed81f3ff0885da627a5fd6c14e/License.md), Git blob SHA-1 `5aefa46c69bc08edf566449260feb5c025d7a39b`. That revision identifies the license text retrieved, not the source revision used to build the NuGet package. Keep upstream notices supplied with this package and its transitive dependencies in redistributed bundles.

## Existing Windows dependencies

The native project also references `Microsoft.WindowsAppSDK` **2.6.5** and `WinUIEx` **2.8.0**. Their applicable upstream licenses and notices continue to apply. Retain the upstream license and notice files supplied with these dependencies when packaging and redistributing them; this document does not assign an unverified license identifier to either dependency. Consult the exact package metadata and supplied upstream notices for the delivered build.

## Update verification scope

PowerToys' Microsoft publisher identity, product identifiers, and Authenticode trust implementation are not adopted. This application's updater verifies its fixed GitHub repository, release metadata, and SHA-256 package integrity. It does **not** perform Authenticode verification or claim that its application packages are signed by Microsoft. Retaining a previous application directory permits manual recovery; it is not an automatic rollback guarantee.
