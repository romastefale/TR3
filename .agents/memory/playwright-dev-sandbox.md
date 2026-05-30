---
name: Playwright Chromium no dev sandbox do Replit
description: Por que renders Playwright não rodam no ambiente de dev deste projeto.
---

O Chromium do Playwright **não vem baixado** no ambiente de dev do Replit, e
`playwright install chromium` estoura o limite de tempo do sandbox (download
grande é morto com exit -1, sem output).

**Consequência:** features que renderizam via Playwright (cards `/tnow`,
`/monthfm`, `/tstory`) só executam o render de fato em produção (Railway), que
tem o browser instalado. No dev, `render_*` cai no fallback (retorna None).

**How to apply:** ao validar features de render no dev, teste as partes que NÃO
dependem do browser (ex.: composição ffmpeg com um PNG gerado via PIL) e confie
na paridade de código com os cards já comprovados em prod. Não bloqueie a
entrega esperando o install do Chromium no dev.
