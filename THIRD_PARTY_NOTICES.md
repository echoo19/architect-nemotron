# Third-party notices

Architect is released under the MIT License (see `LICENSE`). It also contains, adapts or
builds on the work below, each under its own license.

## Hearth (MIT)

These files are adapted from Hearth (https://github.com/echoo19/hearth):

- `desktop/server/originGuard.ts`
- `desktop/src/components/chat/Markdown.tsx`
- `desktop/src/components/chat/useSmoothStream.ts`
- `desktop/src/lib/markdown.ts`
- `desktop/src/lib/shortcuts.ts`
- `desktop/src/lib/useShortcut.ts`

```text
MIT License

Copyright (c) 2026 Hearth Engine Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Lectern design assets (MIT)

Design tokens, motion styles and the mark are © Lectern, contributed under the MIT License by Lectern's owner.

They are:

- `desktop/src/styles/tokens.css`: design tokens
- `desktop/src/styles/motion.css`: motion styles, including the working loader's animation
- `desktop/src/styles/ui.css`: control styles modelled on Lectern's primitives
- `desktop/src/styles/fonts.css`: font setup
- `desktop/src/components/ui/Working.tsx`: the Working loader
- `desktop/build/icon.svg`: the mark, reversed, as the app icon
- `desktop/build/icon.png`: the mark, reversed, as the app icon

```text
MIT License

Copyright (c) Lectern

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Fonts (SIL Open Font License 1.1)

`desktop/src/assets/fonts` holds latin variable WOFF2 builds of 2 families
under the SIL Open Font License 1.1. The license text and each family's copyright
notice are in the `OFL.txt` beside the font files.

- Inter: Copyright 2016 The Inter Project Authors (https://github.com/rsms/inter)
- JetBrains Mono: Copyright 2020 The JetBrains Mono Project Authors (https://github.com/JetBrains/JetBrainsMono)

## Pi (MIT)

Architect's coding backend runs Pi (https://github.com/badlogic/pi-mono), installed
from npm by `architect/backends/pi-node/package.json`: `@mariozechner/pi-agent-core` 0.73.1, `@mariozechner/pi-ai` 0.73.1, `@mariozechner/pi-coding-agent` 0.73.1.

```text
MIT License

Copyright (c) 2025 Mario Zechner

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Tabler Icons (MIT)

The desktop app's icons come from Tabler Icons (https://github.com/tabler/tabler-icons),
`@tabler/icons-react` 3.48.0, bundled into the app build.

```text
MIT License

Copyright (c) 2020-2026 Paweł Kuna

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## CORD v2 (CC BY 4.0)

The receipt cases in `examples/receipts/` are derived from CORD v2
(https://huggingface.co/datasets/naver-clova-ix/cord-v2) by NAVER Clova AI,
described in Park et al., "CORD: A Consolidated Receipt Dataset for Post-OCR
Parsing" (2019), and licensed under CC BY 4.0
(https://creativecommons.org/licenses/by/4.0/). Changes were made: each receipt's
text was rebuilt from CORD's word annotations and its amounts were normalized. No
receipt images are included.
