# TikTok LIVE SaaS MVP — Login + vídeo + início manual

Este pacote implementa o fluxo de produto:

**Entrar com TikTok → enviar vídeo → clicar em Iniciar LIVE → se cair, clicar novamente.**

## O que está pronto

- Login oficial TikTok Login Kit / OAuth 2.0.
- PKCE + `state` anti-CSRF.
- SQLite multiusuário.
- Tokens OAuth criptografados em repouso com Fernet.
- Refresh de access token.
- Perfil básico TikTok.
- Upload de MP4/MOV/MKV/WEBM.
- Histórico de transmissões.
- Botão manual **Iniciar LIVE**.
- Botão **Parar LIVE**.
- FFmpeg em loop.
- Sem watchdog/reinício automático.
- Cliente nunca informa RTMP ou Stream Key.
- Camada de transmissão separada do Login.

## Limitação que permanece

O Login Kit autentica a conta e entrega tokens, mas **não é uma API pública documentada de criação de TikTok LIVE/Stream Key**. Portanto, o pacote não finge que existe um endpoint oficial `create-live -> stream_url + stream_key`.

Para testar o FFmpeg hoje, o MVP possui um **fallback interno/admin** por variáveis de ambiente:

```text
INTERNAL_RTMP_URL=
INTERNAL_STREAM_KEY=
```

Esses valores nunca aparecem na interface do cliente. Eles servem apenas para testar o worker de transmissão quando você já possui um destino RTMP válido.

Quando você tiver um provider de LIVE autorizado, basta substituir `ffmpeg_target()` por esse provider. A interface do cliente não precisa mudar.

## Configuração do TikTok

1. Crie seu app em `developers.tiktok.com`.
2. Adicione Login Kit.
3. Configure a Redirect URI exatamente igual à variável `TIKTOK_REDIRECT_URI`.
4. Para teste inicial, use `user.info.basic`.
5. Copie Client Key e Client Secret.
6. Copie `.env.example` para `.env`.
7. Preencha:

```text
TIKTOK_CLIENT_KEY=...
TIKTOK_CLIENT_SECRET=...
TIKTOK_REDIRECT_URI=http://127.0.0.1:8787/auth/callback/
APP_SECRET=uma-chave-longa-e-aleatoria
```

Para um SaaS real, use HTTPS e uma callback como:

```text
https://seu-dominio.com/auth/callback/
```

## Instalação Windows

```bat
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Instale FFmpeg e deixe `ffmpeg.exe` no PATH.

Defina as variáveis de ambiente ou use um carregador de `.env` de sua preferência. Este MVP não lê `.env` automaticamente para evitar adicionar outra dependência; no Windows você pode usar:

```bat
set TIKTOK_CLIENT_KEY=SEU_CLIENT_KEY
set TIKTOK_CLIENT_SECRET=SEU_CLIENT_SECRET
set TIKTOK_REDIRECT_URI=http://127.0.0.1:8787/auth/callback/
set APP_SECRET=UMA_CHAVE_LONGA
python app.py
```

Depois abra:

`http://127.0.0.1:8787`

## Arquitetura

```text
Cliente
  ↓
Login Kit TikTok
  ↓
SQLite / tokens criptografados
  ↓
Vídeo enviado
  ↓
[ INICIAR LIVE ]
  ↓
Live Provider
  ↓
FFmpeg
  ↓
TikTok
```

## Para colocar em produção

- HTTPS obrigatório para callback web.
- PostgreSQL/MySQL em vez de SQLite.
- Redis + fila de jobs.
- Worker FFmpeg separado do processo web.
- Storage S3/compatível para vídeos.
- Criptografia/secret manager dedicado.
- Rate limiting.
- CSRF em todas as ações mutáveis.
- Limites de tamanho/duração de vídeo.
- Logs centralizados.
- Health checks.
- Um worker por conta/transmissão.
- Um Live Provider real/autorizado para iniciar a LIVE sem RTMP fornecido pelo cliente.
