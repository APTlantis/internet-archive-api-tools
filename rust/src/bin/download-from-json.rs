use std::{fs::File, io::Read, path::PathBuf, time::Duration};

use anyhow::{Context, Result};
use clap::Parser;
use indicatif::{ProgressBar, ProgressStyle};
use regex::Regex;
use reqwest::{Client, StatusCode};
use serde::Deserialize;
use tokio::{fs, io::AsyncWriteExt};

#[derive(Parser, Debug)]
#[command(name = "download-from-json", about = "Download files from a JSON list (Rust)")]
struct Args {
    /// Path to JSON metadata file (list of items)
    #[arg(short, long, default_value = "iso_metadata.json")]
    input: PathBuf,
    /// Destination directory for downloads
    #[arg(short, long, default_value = "S:/Linux-FUCKIN-ISOs/")]
    output_dir: PathBuf,
    /// Total HTTP retries for transient errors
    #[arg(long, default_value_t = 5)]
    retries: usize,
    /// Request timeout seconds
    #[arg(long, default_value_t = 60)]
    timeout: u64,
    /// Retry backoff factor
    #[arg(long, default_value_t = 1.0)]
    backoff: f32,
    /// Download chunk size in bytes
    #[arg(long, default_value_t = 1024 * 256)]
    chunk_size: usize,
    /// Resume partially downloaded files using HTTP Range
    #[arg(long)]
    resume: bool,
    /// Disable per-file progress bar output
    #[arg(long)]
    no_progress: bool,
    /// Do not download, just list actions
    #[arg(long)]
    dry_run: bool,
    /// Limit the number of items to process
    #[arg(long, default_value_t = 0)]
    max: usize,
    /// Regex that file_name/title must match to be downloaded
    #[arg(long)]
    include: Option<String>,
    /// Regex that if matched will skip the item
    #[arg(long)]
    exclude: Option<String>,
    /// Custom User-Agent header
    #[arg(long)]
    user_agent: Option<String>,
    /// Increase verbosity (-v info, -vv debug)
    #[arg(short = 'v', action = clap::ArgAction::Count)]
    verbosity: u8,
}

#[derive(Debug, Deserialize)]
struct Item {
    file_name: Option<String>,
    download_url: Option<String>,
    title: Option<String>,
}

#[tokio::main(flavor = "multi_thread")]
async fn main() -> Result<()> {
    let args = Args::parse();

    // Read JSON file
    let mut f = File::open(&args.input).with_context(|| format!("Failed to open {}", args.input.display()))?;
    let mut buf = String::new();
    f.read_to_string(&mut buf)?;
    let mut data: Vec<Item> = serde_json::from_str(&buf).context("Input JSON must be a list of items")?;

    // Filter by include/exclude
    let inc_re = if let Some(s) = &args.include { Some(Regex::new(&format!("(?i){}", s))?) } else { None };
    let exc_re = if let Some(s) = &args.exclude { Some(Regex::new(&format!("(?i){}", s))?) } else { None };
    data = data.into_iter().filter(|it| {
        let name = format!("{} {}", it.file_name.clone().unwrap_or_default(), it.title.clone().unwrap_or_default());
        if let Some(re) = &inc_re { if !re.is_match(&name) { return false; } }
        if let Some(re) = &exc_re { if re.is_match(&name) { return false; } }
        true
    }).collect();

    if args.max > 0 && data.len() > args.max { data.truncate(args.max); }

    // Prepare destination
    fs::create_dir_all(&args.output_dir).await?;

    let client = Client::builder()
        .user_agent(args.user_agent.clone().unwrap_or_else(|| "Internet-Archive-API/rs".to_string()))
        .timeout(Duration::from_secs(args.timeout))
        .build()?;

    let total = data.len();
    eprintln!("Total items to process: {}", total);

    let show_progress = !args.no_progress && atty::is(atty::Stream::Stdout);

    let mut success = 0usize;
    let mut skipped = 0usize;
    let mut failed = 0usize;

    for (idx, item) in data.into_iter().enumerate() {
        let file_name = match item.file_name { Some(s) if !s.is_empty() => s, _ => { failed += 1; continue; } };
        let url = match item.download_url { Some(s) if !s.is_empty() => s, _ => { failed += 1; continue; } };

        let dest_path = args.output_dir.join(&file_name);
        let prefix = format!("[{}/{} {:.1}%]", idx + 1, total, ((idx + 1) as f32 / total as f32) * 100.0);

        if dest_path.exists() {
            eprintln!("{} Already exists: {}", prefix, file_name);
            skipped += 1;
            continue;
        }

        if args.dry_run {
            println!("{} [DRY-RUN] Would download: {} <- {}", prefix, file_name, url);
            skipped += 1;
            continue;
        }

        match download_with_retries(&client, &url, &dest_path, args.chunk_size, args.retries, args.backoff, args.resume, show_progress).await {
            Ok(()) => { println!("{} [✔] Done: {}", prefix, file_name); success += 1; }
            Err(e) => { eprintln!("{} [✗] Failed: {} - {}", prefix, file_name, e); failed += 1; }
        }
    }

    eprintln!("Completed. Success: {}, Skipped: {}, Failed: {}", success, skipped, failed);
    Ok(())
}

async fn download_with_retries(client: &Client, url: &str, dest_path: &PathBuf, chunk_size: usize, retries: usize, backoff: f32, resume: bool, show_progress: bool) -> Result<()> {
    let mut attempt = 0usize;
    loop {
        attempt += 1;
        match download_once(client, url, dest_path, chunk_size, resume, show_progress).await {
            Ok(_) => return Ok(()),
            Err(e) => {
                if attempt > retries { return Err(e); }
                let sleep = backoff * attempt as f32;
                tokio::time::sleep(Duration::from_secs_f32(sleep)).await;
            }
        }
    }
}

async fn download_once(client: &Client, url: &str, dest_path: &PathBuf, chunk_size: usize, resume: bool, show_progress: bool) -> Result<()> {
    let mut downloaded: u64 = 0;
    let mut headers = reqwest::header::HeaderMap::new();
    let mut mode = "create"; // or append

    if resume {
        if let Ok(meta) = fs::metadata(dest_path).await {
            downloaded = meta.len();
            if downloaded > 0 {
                headers.insert(reqwest::header::RANGE, format!("bytes={}-", downloaded).parse().unwrap());
                mode = "append";
            }
        }
    }

    let resp = client.get(url).headers(headers).send().await?;
    if !(resp.status() == StatusCode::OK || resp.status() == StatusCode::PARTIAL_CONTENT) {
        let status = resp.status();
        let text = resp.text().await.unwrap_or_default();
        anyhow::bail!("HTTP {}: {}", status, text);
    }

    let total_opt = resp.headers().get(reqwest::header::CONTENT_LENGTH).and_then(|h| h.to_str().ok()).and_then(|s| s.parse::<u64>().ok()).map(|n| if resp.status() == StatusCode::PARTIAL_CONTENT { n + downloaded } else { n });

    let pb = if show_progress {
        let pb = ProgressBar::new(total_opt.unwrap_or(0));
        pb.set_style(ProgressStyle::with_template("{prefix} [{bar:40.cyan/blue}] {bytes}/{total_bytes} ({eta})").unwrap());
        pb.set_prefix(format!("[↓] {}", dest_path.file_name().and_then(|s| s.to_str()).unwrap_or("file")));
        if let Some(t) = total_opt { pb.set_length(t); }
        if downloaded > 0 { pb.set_position(downloaded); }
        Some(pb)
    } else { None };

    let mut stream = resp.bytes_stream();
    let mut file = if mode == "append" { fs::OpenOptions::new().append(true).open(dest_path).await? } else { fs::File::create(dest_path).await? };

    use futures::StreamExt;
    if chunk_size == 0 {
        // Fallback: write through directly
        while let Some(chunk) = stream.next().await {
            let bytes = chunk?;
            file.write_all(&bytes).await?;
            downloaded += bytes.len() as u64;
            if let Some(pb) = &pb { pb.set_position(downloaded); }
        }
    } else {
        let mut buffer: Vec<u8> = Vec::with_capacity(chunk_size);
        while let Some(chunk) = stream.next().await {
            let bytes = chunk?;
            buffer.extend_from_slice(&bytes);
            // Write out in fixed-size chunks
            let mut start = 0usize;
            while buffer.len() - start >= chunk_size {
                let end = start + chunk_size;
                file.write_all(&buffer[start..end]).await?;
                downloaded += chunk_size as u64;
                if let Some(pb) = &pb { pb.set_position(downloaded); }
                start = end;
            }
            // Retain the remainder in buffer
            if start > 0 {
                buffer.drain(0..start);
            }
        }
        // Flush any remaining bytes
        if !buffer.is_empty() {
            file.write_all(&buffer).await?;
            downloaded += buffer.len() as u64;
            if let Some(pb) = &pb { pb.set_position(downloaded); }
        }
    }
    if let Some(pb) = &pb { pb.finish_and_clear(); }

    Ok(())
}
