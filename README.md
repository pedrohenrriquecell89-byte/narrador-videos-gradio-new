# Video Auto — narração automática de vídeo (Render free tier)

Roteiro em inglês + fotos em sequência → vídeo narrado (1920x1080, H.264),
pronto pra YouTube. Uso único (sem login), rodando de graça no Render.

## Como funciona a sincronia

O roteiro é dividido em **N blocos de texto** (N = número de fotos enviadas),
balanceados por contagem de palavras sem cortar frases. Cada bloco vira um
áudio próprio via **Piper TTS**. Cada foto fica em tela exatamente pela
duração do **seu** áudio. Isso garante sincronia perfeita por construção,
sem precisar de transcrição/alinhamento depois (WhisperX) — o que também
economiza muita memória, essencial no plano gratuito do Render (512MB RAM).

Se uma foto está corrompida ou inválida, o upload inteiro é **rejeitado
antes de processar** com uma mensagem clara — não há processamento
desperdiçado, e não há caso de "sem imagem correspondente" porque a
validação acontece antes de qualquer bloco ser criado.

## Estrutura do projeto

```
videoauto/
├── Dockerfile           # ffmpeg + piper + voz + deps python
├── requirements.txt
├── render.yaml          # config de deploy (opcional, "Blueprint")
├── app.py               # backend FastAPI (upload, TTS, render, download)
├── templates/
│   └── index.html       # interface mobile-first
└── static/
    ├── style.css
    └── script.js
```

## Limitações importantes do plano gratuito do Render (leia antes)

- **512MB de RAM, CPU compartilhada.** Vídeos de até ~5-6 minutos devem
  rodar sem problemas. Perto do limite de 10 minutos, o processo de
  renderização pode ficar lento ou, em casos extremos, ser encerrado por
  falta de memória — se isso acontecer, tente um roteiro mais curto.
- **O serviço "dorme"** após ~15 minutos sem tráfego. A primeira visita
  depois disso demora 30-60s pra responder (cold start). Isso é esperado
  e a interface já avisa o usuário sobre isso.
- **Disco efêmero.** Os arquivos de cada job (fotos, áudio, vídeo final)
  ficam apenas no contêiner em execução. Se o serviço reiniciar ou
  reimplantar, jobs em andamento são perdidos. Jobs concluídos com mais
  de 2 horas são limpos automaticamente para não estourar o disco.
- **750h/mês grátis** por serviço — suficiente para uso pessoal esporádico.

## Passo a passo de deploy no Render

1. **Crie uma conta** em https://render.com (pode usar login com GitHub).
2. **Suba este projeto para um repositório no GitHub** (crie um repo novo,
   ex. `videoauto`, e faça push de todos os arquivos desta pasta).
3. No painel do Render, clique em **New +** → **Web Service**.
4. Conecte sua conta do GitHub e selecione o repositório `videoauto`.
5. Configure:
   - **Environment**: `Docker` (o Render detecta o `Dockerfile` automaticamente)
   - **Plan**: `Free`
   - **Region**: a mais próxima de você
   - Não é necessário configurar Build/Start Command manualmente — tudo
     está definido no `Dockerfile` (o `CMD` já sobe o `uvicorn`).
6. Clique em **Create Web Service**. O primeiro build demora alguns
   minutos (baixa o Piper e o modelo de voz, ~65MB).
7. Quando o status ficar **Live**, acesse a URL fornecida pelo Render
   (algo como `https://videoauto-xxxx.onrender.com`) pelo celular.

### Alternativa: deploy via Blueprint (render.yaml)

Se preferir, no painel do Render use **New +** → **Blueprint**, aponte
para o repositório — o arquivo `render.yaml` incluso já define o serviço
automaticamente.

## Uso

1. Abra o link do Render no celular.
2. Cole o roteiro em inglês na caixa de texto.
3. Selecione as fotos **na ordem em que devem aparecer no vídeo** (a
   maioria dos seletores de galeria mantém a ordem de seleção).
4. Toque em **Gerar vídeo** e aguarde a barra de progresso.
5. Quando pronto, toque em **Baixar vídeo**.

## Ajustando a voz (opcional)

No `app.py`, dentro de `process_job`, os parâmetros do Piper controlam a
entonação:
- `--length_scale`: velocidade da fala (menor = mais rápido). Padrão `1.0`.
- `--noise_scale` / `--noise_w`: variação natural da voz. Padrão `0.667` / `0.8`.

## Troubleshooting

- **Erro "Comando não encontrado: piper"** → o build do Docker não baixou
  o binário corretamente; verifique os logs de build no Render.
- **Vídeo com erro de sincronia** → não deveria acontecer (a duração de
  cada cena vem diretamente do áudio gerado), mas se notar algo estranho,
  veja `jobs/<id>/job.log` dentro do contêiner (via Shell do Render) para
  detalhes.
- **Erro de memória em vídeos longos** → reduza o roteiro para menos de
  ~1200 palavras (~8 minutos de narração).
