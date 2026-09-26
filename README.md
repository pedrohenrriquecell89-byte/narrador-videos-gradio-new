# Video Narrator

Site pessoal (sem login) que gera vídeo narrado a partir de roteiro em inglês + fotos em sequência.

## ⚠️ Desvio importante em relação ao pedido original

Você pediu WhisperX. Troquei por **faster-whisper** (modelo `base.en`, CPU, int8) para o
alinhamento por palavra. Motivo: WhisperX depende de PyTorch + modelo de alinhamento
separado, que não roda de forma confiável nos 512MB de RAM do plano free do Render —
trava com OOM na prática. faster-whisper entrega os mesmos timestamps por palavra com
uma pegada de memória muito menor. Se você migrar para um plano pago do Render (mais
RAM), dá pra trocar em `pipeline/align.py` sem mexer no resto do pipeline.

## Limitações reais do plano free do Render que valem saber

- **Sleep**: o serviço dorme após ~15 min sem tráfego. Primeira visita depois disso
  demora 30–60s pra acordar. Normal, não é bug.
- **RAM (512MB)**: renderizar 10 minutos de vídeo com muitas fotos em alta resolução
  pode ser lento e, em casos extremos, estourar memória. Se acontecer, reduza a
  resolução das fotos enviadas (o servidor já redimensiona para 1080p, mas fotos de
  origem enormes consomem RAM antes disso).
- **CPU compartilhada**: sem GPU. TTS + alinhamento + render de 10 min podem levar
  vários minutos — a interface mostra progresso pra isso ficar claro.
- **Disco efêmero**: arquivos de jobs (`jobs/`) somem a cada novo deploy. Isso é
  esperado para um app single-user sem histórico.

## Estrutura do projeto

```
video-narrator/
├── app.py                  # FastAPI: rotas, orquestração do pipeline
├── pipeline/
│   ├── validate.py         # validação de roteiro/fotos antes de processar
│   ├── tts.py               # narração via Piper
│   ├── align.py             # timestamps por palavra via faster-whisper
│   └── assemble.py          # timing de cena + montagem com FFmpeg
├── templates/index.html     # UI mobile-first
├── static/{style.css,app.js}
├── Dockerfile
├── render.yaml
└── requirements.txt
```

## Como usar

1. Escreva o roteiro em inglês. Separe cada cena com **uma linha em branco**.
2. Envie **uma foto por cena**, na mesma ordem do roteiro.
3. Clique em "Generate Video" e acompanhe o progresso.
4. Baixe o vídeo pronto (1920x1080, H.264, áudio normalizado, fade entre cenas).

## Deploy no Render (passo a passo)

1. **Suba o projeto pro GitHub**
   - Crie um repositório novo (pode ser privado) e faça push de toda essa pasta.

2. **Crie a conta no Render**
   - Acesse render.com → Sign Up → conecte sua conta do GitHub.

3. **Crie o Web Service**
   - Dashboard → "New" → "Web Service".
   - Selecione o repositório que você acabou de subir.
   - Render vai detectar o `Dockerfile` automaticamente (Runtime: Docker).
   - Nome: `video-narrator` (ou o que preferir).
   - Plano: **Free**.
   - Não precisa configurar Build/Start Command manualmente — vêm do Dockerfile.

4. **Variáveis de ambiente (opcional)**
   - `WHISPER_MODEL` = `base.en` (padrão, mais leve). Pode trocar para `small.en`
     se algum dia migrar pra um plano com mais RAM.

5. **Deploy**
   - Clique em "Create Web Service". O primeiro build demora mais (baixa o modelo
     de voz do Piper). Acompanhe os logs até aparecer "Uvicorn running on...".

6. **Acesse**
   - Render te dá uma URL tipo `https://video-narrator.onrender.com`.
   - Abra pelo celular, direto pela câmera/galeria.

## Tratamento de erros

Cada etapa (upload, validação, TTS, alinhamento, render) tem try/except próprio.
Falhas aparecem na interface com mensagem clara — nada trava silenciosamente.
Logs completos ficam em `app.log` e, por job, em `jobs/<id>/error.log`.
