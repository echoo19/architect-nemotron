# Vendored files

| Path | What | Source | sha256 |
|---|---|---|---|
| `tabler-icons/LICENSE` | the MIT licence of the Tabler Icons paths in `design/components.js` and `src/icons.mjs` | `@tabler/icons@3.48.0` | `b740a1d46122672da62833e97f7e7c8a13fa85cbc7445b584b297cc00dde93db` |
| `fonts/Inter-OFL.txt` | the SIL Open Font License of Inter | the OFL 1.1 text with the copyright line of `rsms/inter` | `8c8fa881d9091ee315d2c80d207bb73afd4e90a66c9782ca445c5642f25db6dc` |
| `fonts/JetBrainsMono-OFL.txt` | the SIL Open Font License of JetBrains Mono | `@fontsource-variable/jetbrains-mono@5.3.0` | `403581b69dac5cff4079205e01c6b467e56af449ecbd7247693ddb1baafa005b` |

The font files themselves are not vendored here. The build copies them from
`desktop/src/assets/fonts`, so the site and the desktop app use the same files.
`NOTICES.txt` in the built site carries every licence above.
