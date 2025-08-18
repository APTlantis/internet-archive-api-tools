use std::{path::PathBuf, time::Duration};

use anyhow::{Context, Result};
use clap::Parser;
use globset::{Glob, GlobMatcher};
use percent_encoding::{utf8_percent_encode, AsciiSet, CONTROLS};
use reqwest::Client;
use serde_json::Value;
use tokio::{fs, io::AsyncWriteExt};

const DOWNLOAD_BASE_URL: &str = "https://archive.org/download";
const METADATA_BASE_URL: &str = "https://archive.org/metadata/";

const FRAGMENT: &AsciiSet = &CONTROLS
    .add(b' ').add(b'"').add(b'<').add(b'>').add(b'`')
    .add(b'#').add(b'?').add(b'{').add(b'}');

#[derive(Parser, Debug)]
#[command(name = "download-collections", about = "Download an entire Internet Archive item (Rust)")]
struct Args {
    /// Archive.org item identifier
    identifier: String,
    /// Destination directory
    #[arg(short = 'o', long, default_value = "S:/Linux-FUCKIN-ISOs")] 
    destdir: PathBuf,
    /// Skip files that already exist (default: true)
    #[arg(long, default_value_t = true)]
    ignore_existing: bool,
    /// Do not skip existing files
    #[arg(long = "no-ignore-existing")] 
    no_ignore_existing: bool,
    /// Verify checksums after download (not implemented; placeholder)
    #[arg(long)]
    checksum: bool,
    /// Number of retries
    #[arg(long, default_value_t = 5)]
    retries: usize,
    /// Only download files matching this glob pattern (e.g. *.iso)
    #[arg(long)]
    glob: Option<String>,
    /// Optional log verbosity
    #[arg(short = 'v', action = clap::ArgAction::Count)]
    verbosity: u8,
    /// Dry run: list files without downloading
    #[arg(long)]
    dry_run: bool,
}

#[tokio::main(flavor = "multi_thread")]
async fn main() -> Result<()> {
    let mut args = Args::parse();
    if args.no_ignore_existing { args.ignore_existing = false; }

    let client = Client::builder()
        .user_agent("Internet-Archive-API/rs")
        .timeout(Duration::from_secs(60))
        .build()?;

    fs::create_dir_all(&args.destdir).await?;

    let meta_url = format!("{}{}", METADATA_BASE_URL, &args.identifier);
    let meta: Value = client.get(&meta_url).send().await?.json().await
        .with_context(|| format!("Failed to fetch metadata for {}", &args.identifier))?;

    let files = meta.get("files").and_then(|v| v.as_array()).cloned().unwrap_or_default();

    let matcher: Option<GlobMatcher> = if let Some(pattern) = &args.glob { 
        Some(Glob::new(pattern)?.compile_matcher()) 
    } else { None };

    for f in files {
        let name = match f.get("name").and_then(|v| v.as_str()) { Some(s) => s, None => continue };
        if let Some(m) = &matcher { if !m.is_match(name) { continue; } }

        let url = format!("{}/{}/{}", DOWNLOAD_BASE_URL, &args.identifier, encode_path_segment(name));
        let dest_path = args.destdir.join(name);

        if args.dry_run {
            println!("{}", name);
            continue;
        }

        if args.ignore_existing && dest_path.exists() { 
            if args.verbosity >= 1 { eprintln!("Skip existing: {}", name); }
            continue; 
        }

        if let Err(e) = download_with_retries(&client, &url, &dest_path, args.retries).await {
            eprintln!("Failed {}: {}", name, e);
        }
    }

    if args.verbosity >= 1 { eprintln!("Download finished"); }
    Ok(())
}

async fn download_with_retries(client: &Client, url: &str, dest: &PathBuf, retries: usize) -> Result<()> {
    let mut attempt = 0usize;
    loop {
        attempt += 1;
        let res = download_once(client, url, dest).await;
        match res {
            Ok(()) => return Ok(()),
            Err(e) => {
                if attempt > retries { return Err(e); }
                tokio::time::sleep(Duration::from_secs(attempt as u64)).await;
            }
        }
    }
}

async fn download_once(client: &Client, url: &str, dest: &PathBuf) -> Result<()> {
    let resp = client.get(url).send().await?;
    if !resp.status().is_success() { 
        let status = resp.status();
        let text = resp.text().await.unwrap_or_default();
        anyhow::bail!("HTTP {}: {}", status, text);
    }

    let mut file = tokio::fs::File::create(dest).await?;
    let mut stream = resp.bytes_stream();
    use futures::StreamExt;
    while let Some(chunk) = stream.next().await {
        let bytes = chunk?;
        file.write_all(&bytes).await?;
    }
    Ok(())
}

fn encode_path_segment(s: &str) -> String {
    utf8_percent_encode(s, FRAGMENT).to_string()
}
