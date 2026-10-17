# PDF.js 6.4.299

Source: https://www.npmjs.com/package/pdfjs-dist/v/6.4.299
Upstream: https://github.com/mozilla/pdf.js/tree/v6.4.299
License: Apache-2.0; see LICENSE and the individual font/CMap/WASM/ICC licenses.

The npm tarball was fetched with `npm pack pdfjs-dist@6.4.299 --ignore-scripts`.
Verified tarball integrity:

`sha512-AVl138zALtfaAPvADulE0PZThbYzCBS79nL4pOSL/6Sm/4AH5A21BD9VHt97OlCuzJuCpmeZtAtkinisF4Vb1g==`

Included unchanged: legacy display/worker builds, the legacy viewer component
and CSS, viewer icons, CMaps, standard fonts, WASM decoders and ICC resources.
Source maps, types, Node dependencies, sample PDFs and the editable generic
viewer application are not required or included. The CFM shell and controls
live outside this folder. No npm install/build or CDN is needed at runtime.

To update, verify the new official tarball, replace this pinned folder, update
all versioned references and this notice, and run the full regression/browser
gate. Display, worker and viewer versions must always match.
