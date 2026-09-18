"""Startup adapters run solely inside the disposable preview container."""

from __future__ import annotations

import json
import shlex
from uuid import UUID


def preview_command(
    framework: str, port: int, session_id: UUID | str, custom_command: str = ""
) -> str:
    """Use a stable base path; the gateway inserts scoped viewing tickets."""
    if framework not in {"static", "http", "vite", "next"} or not 1024 <= port <= 65535:
        raise ValueError("Unsupported preview framework or port")
    stable = f"/runtime/previews/{UUID(str(session_id))}"
    environment = (
        f"NEXT_TELEMETRY_DISABLED=1 PORT={port} HOST=127.0.0.1 "
        f"HOSTNAME=127.0.0.1 JHIN_PREVIEW_BASE={shlex.quote(stable)} "
    )
    if custom_command:
        if len(custom_command) > 8000 or "\x00" in custom_command:
            raise ValueError("Invalid preview command")
        return environment + "bash --noprofile --norc -c " + shlex.quote(custom_command)
    if framework in {"static", "http"}:
        return f"python -I -m http.server {port} --bind 127.0.0.1 --directory /app"
    install = "npm install --no-audit --no-fund && "
    if framework == "vite":
        script = (
            "import('vite').then(async ({createServer})=>{const server=await createServer({"
            f"base:{json.dumps(stable + '/')},server:{{host:'127.0.0.1',port:{port},"
            "strictPort:true,allowedHosts:['localhost','127.0.0.1'],"
            "hmr:{clientPort:undefined}}});await server.listen();server.printUrls();"
            "}).catch(e=>{console.error(e);process.exit(1)})"
        )
    else:
        # Next's custom-server wrapper does not propagate the conf option to
        # its development router. Install a wrapper config in disposable /app
        # so every worker reads the same basePath, retaining the user's config.
        wrapper = (
            "import fs from 'node:fs';import path from 'node:path';"
            "import {createRequire} from 'node:module';"
            "const require=createRequire(import.meta.url);"
            "let original={};const n=fs.readdirSync('.')"
            ".find(n=>n.startsWith('.jhin-original-next.config.'));"
            "if(n){if(n.endsWith('.ts')){const {transpileConfig}="
            "require('next/dist/build/next-config-ts/transpile-config');"
            "original=await transpileConfig({nextConfigPath:path.resolve(n),"
            "dir:process.cwd(),cwd:process.cwd()});}"
            "else{original=(await import('./'+n)).default;}}"
            "export default async(...args)=>({...((typeof original==='function')?"
            "await original(...args):await original),"
            f"basePath:{json.dumps(stable)},assetPrefix:{json.dumps(stable)}}});"
        )
        script = (
            "const fs=require('node:fs');"
            "for(const n of ['next.config.js','next.config.mjs','next.config.ts'])"
            "{if(fs.existsSync(n))fs.renameSync(n,'.jhin-original-'+n);}"
            f"fs.writeFileSync('next.config.mjs',{json.dumps(wrapper)});"
        )
        return (
            install
            + environment
            + "node -e "
            + shlex.quote(script)
            + " && "
            + environment
            + f"node node_modules/next/dist/bin/next dev --hostname 127.0.0.1 --port {port}"
        )
    return install + environment + "node -e " + shlex.quote(script)
