# Third-party notices

The Apache License 2.0 in this repository applies to AutoCAD Mechanical Harness source code
and documentation. Third-party software keeps its own license and trademark terms.

## Runtime and development dependencies

The Python environment is resolved by `pyproject.toml` and `uv.lock`. Direct dependencies
include software distributed under MIT, BSD, Apache, Python Software Foundation, and
LGPL/GPL/commercial license options. In particular, the optional Engineer Desktop uses
PySide6; redistributors are responsible for complying with the applicable Qt/PySide license.

The C# test environment uses NuGet packages declared in the project files, including the
Microsoft test SDK, xUnit, Newtonsoft.Json, and related transitive packages. Those packages
are not relicensed by this project.

## Autodesk SDK and AutoCAD

The AutoCAD plugin has a build-time reference to `AutoCAD.NET`, which provides Autodesk
ObjectARX/AutoCAD managed API assemblies under Autodesk's separate license terms. Restoring
or using that package does not place Autodesk software under Apache-2.0.

The repository and its release packaging path do not vendor Autodesk runtime DLLs. Building
and running the live plugin requires the appropriate Autodesk SDK terms and a separately
licensed, compatible AutoCAD installation. AutoCAD and AutoCAD Mechanical are Autodesk
trademarks; this project is independent and not endorsed by Autodesk.

Dependency manifests and the license files shipped by each dependency are authoritative.
This notice is informational and is not legal advice.
