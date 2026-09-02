# Third-party notices

## Project and artwork

Nagi original source, documentation and the simple vector N mark in
`nagi/web_ui/nagi-avatar.svg` are distributed under the root MIT LICENSE.
This grant does not cover imported games, scripts, screenshots, characters,
translation results or third-party software.

`nagi/web_ui/nagi-avatar.png` and the reference artwork used to create
`assets/nagi-icon.png` / `assets/nagi-icon.ico` were provided by the
repository owner. The icon is a background-cleaned derivative generated at the
owner's direction for the Nagi Windows launcher. These files are not licensed
under this project's MIT grant; copyright remains with their respective rights
holders. The old CLI screenshots remain excluded from release inputs.

## Controlled Windows runtime

The Windows x64 bundle uses official Python 3.13.15 embeddable binaries.
URL and SHA-256 are pinned in `release/components.json` against Python's
[official release manifest](https://www.python.org/ftp/python/3.13.15/windows-3.13.15.json).
The upstream LICENSE.txt (PSF license and incorporated third-party notices)
is preserved both in runtime and in licenses/Python-LICENSE.txt.
See [Python license](https://docs.python.org/3.13/license.html).

The portable feature set is base Agent, translation, BM25, MCP and Frida.
`uv.lock` and `requirements/portable.lock` pin every direct/transitive dependency
and artifact hashes. Each wheel's license/notice files are retained in place
and copied into `licenses/<distribution>-<version>/`; licenses/components.json
lists the actual assembled components, versions and associated notice files.
The builder refuses a package with no license text. This inventory includes
the MCP SDK and jsonschema, Pydantic, HTTPX/HTTPCore, Starlette/Uvicorn, AnyIO,
cryptography/CFFI, pywin32, Frida and their actual transitive dependencies.
No installed development environment is a release input.

MCP SDK: [MIT](https://github.com/modelcontextprotocol/python-sdk/blob/main/LICENSE).
Frida 17.17.0 wheel: wxWindows Library Licence 3.1, including its binary
distribution exception; the GNU Library GPL v2 text it references is included
at `release/licenses/Frida-LGPL-2.0.txt` (portable: licenses/supplemental).
See [upstream COPYING](https://github.com/frida/frida/blob/main/COPYING);
the unmodified frida-python wheel's own license texts and incorporated-component
notices are shipped with the binary. Consult those exact files, not a generic
package-category label, when redistributing a modified build.

Source-only users obtain dependencies from upstream under the dependency's
license. Optional RAG wheels are not part of this portable release; their
upstream license texts remain in installed distribution metadata. No pretrained
weights, Locale Emulator binaries or privately built YPF archives are included.

## Locale Emulator (optional, user-provided)

Nagi does not bundle or download Locale Emulator. Users select their own local
installation in the translation workbench. New launchers invoke the external
LEProc executable with an independent per-game Japanese profile; the user's
global profile GUID is not assumed. Existing private deployments remain compatible.

Upstream: https://github.com/xupefei/Locale-Emulator
Core: https://github.com/xupefei/Locale-Emulator-Core
The upstream projects identify their main/core source as LGPL-3.0; the main
project also documents separately licensed components (including MS-PL code).
If distributing third-party binaries in a separate installer in the future,
retain the applicable licenses/notices and satisfy the corresponding-source
requirements. Selecting a local executable does not make it part of this repository.

## Local YU-RIS deployment experiment

Game resources and Locale Emulator binaries are not bundled with Nagi. The
verified-pilot deployment reuses a hash-pinned local experiment and copies a
private game instance, without redistributing those assets in this repository.
The prebuilt YPF artifact was produced using the locally adapted
dreamsavior/ypf-repacker tool; its source and MIT notice remain in the research
checkout. Retain the game/tool authors' notices when managing those local files.
The optional deployment dependency is Frida 17.17.0. Nagi's launcher and Unicode
drawing bridge templates are maintained in `nagi/gameio/deployment_runtime/`.

## Optional local retrieval models

Model files are not bundled in this source repository. The explicit provisioning
command downloads pinned files from their authors and verifies SHA-256 digests.

- intfloat/multilingual-e5-small (MIT):
  https://huggingface.co/intfloat/multilingual-e5-small
- cross-encoder/mmarco-mMiniLMv2-L12-H384-v1 (Apache-2.0):
  https://huggingface.co/cross-encoder/mmarco-mMiniLMv2-L12-H384-v1

Keep the upstream license/attribution terms when redistributing model weights.
Local inference uses ONNX Runtime, Hugging Face Tokenizers and NumPy under their
respective licenses; these are optional dependencies, not vendored code.

## GARbro

Parts of Nagi's QLIE FilePackVer3.0 format implementation were independently
reimplemented with reference to GARbro's QLIE archive, encryption, and modified
Mersenne Twister sources.

Project: https://github.com/morkt/GARbro

Copyright (c) 2014-2020 morkt

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
the Software, and to permit persons to whom the Software is furnished to do so,
subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
