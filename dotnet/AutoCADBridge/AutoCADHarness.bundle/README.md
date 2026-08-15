# AutoCADHarness.bundle

This manifest targets AutoCAD 2025 (`R25.0`) on .NET 8. The release build copies the
signed plugin and bridge assemblies to `Contents/Windows/`.

AutoCAD 2026 (`R25.1`) is deliberately excluded from this manifest. Autodesk changed
AutoCAD 2026 Update 1.2 and later to .NET 10, while the original release through Update
1.1 uses .NET 8. A separately identified 2026 artifact must be built and live-tested for
the exact update/runtime before its compatibility policy can move beyond provisional.

The committed `maintainers@cad-harness.local` contact is a non-routable development
placeholder. Release packaging must replace it with the deploying organisation's support
address, sign every distributed binary/checksum, and reject packaging if either condition
is unmet. AutoCAD 2024 uses .NET Framework 4.8 and requires a separately built bundle; it
must not load this .NET 8 artifact.

Create a deliberately unsigned local smoke bundle with an explicit release identity:

```powershell
.\Package-BridgeBundle.ps1 -TargetFramework net8.0-windows `
  -AutoCADManagedApiVersion 25.0.1 -AutoCADSeries R25.0 `
  -PackageVersion 0.2.2.0 `
  -ProductCode 82658044-ebc3-4ecd-928c-a5b96770fc96 `
  -DevelopmentUnsigned
```

Every later release must increment `PackageVersion` and use a new `ProductCode`; the
manifest's `UpgradeCode` remains stable. Signed release mode additionally requires the
organization support email, a current code-signing certificate thumbprint, and an approved
HTTPS Authenticode timestamp server.
