use std::{thread, time::Duration, fs::File, io::Write};

use anyhow::{Context, Result};
use clap::Parser;
use reqwest::{Client, StatusCode};
use serde::{Deserialize, Serialize};
use serde_json::Value;

const SEARCH_URL: &str = "https://archive.org/advancedsearch.php";
const METADATA_BASE_URL: &str = "https://archive.org/metadata/";
const DOWNLOAD_BASE_URL: &str = "https://archive.org/download";

#[derive(Parser, Debug)]
#[command(name = "ia-advanced-search", about = "Internet Archive Advanced Search (Rust)")]
struct Args {
    /// Advanced search query string
    #[arg(short, long, default_value = "(format:ISO OR format:IMG) AND mediatype:software AND description:\"linux, distribution\"")]
    query: String,
    /// Rows per page (<=1000)
    #[arg(long, default_value_t = 500)]
    rows: usize,
    /// Limit number of pages to fetch
    #[arg(long)]
    max_pages: Option<usize>,
    /// Sleep seconds between requests
    #[arg(long, default_value_t = 1.0)]
    sleep: f32,
    /// Fields to fetch in search results
    #[arg(long, num_args = 1.., value_delimiter = ' ', default_values_t = ["identifier".to_string(), "title".to_string(), "date".to_string(), "creator".to_string()])]
    fields: Vec<String>,
    /// Output JSON file for results
    #[arg(short, long, default_value = "iso_metadata.json")]
    out: String,
    /// Request timeout seconds
    #[arg(long, default_value_t = 30)]
    timeout: u64,
    /// HTTP retries for transient errors
    #[arg(long, default_value_t = 5)]
    retries: usize,
    /// Retry backoff factor
    #[arg(long, default_value_t = 1.0)]
    backoff: f32,
    /// Custom User-Agent header
    #[arg(long)]
    user_agent: Option<String>,
    /// Increase verbosity (-v info, -vv debug)
    #[arg(short = 'v', action = clap::ArgAction::Count)]
    verbosity: u8,
    /// Do not fetch per-item metadata, only list identifiers
    #[arg(long)]
    dry_run: bool,
}

#[derive(Debug, Deserialize)]
struct SearchResponse {
    response: Option<SearchInner>,
    #[allow(dead_code)]
    error: Option<Value>,
}

#[derive(Debug, Deserialize)]
struct SearchInner {
    #[serde(default, rename = "numFound")]
    num_found: i64,
    #[serde(default)]
    docs: Vec<serde_json::Map<String, Value>>, // flexible
}

#[derive(Debug, Serialize, Deserialize)]
struct IsoEntry {
    identifier: String,
    title: String,
    file_name: String,
    download_url: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    size: Option<String>,
}

#[tokio::main(flavor = "multi_thread")]
async fn main() -> Result<()> {
    let args = Args::parse();

    let client = Client::builder()
        .user_agent(args.user_agent.clone().unwrap_or_else(|| "Internet-Archive-API/rs".to_string()))
        .timeout(Duration::from_secs(args.timeout))
        .build()?;

    if args.verbosity >= 1 {
        eprintln!("Query: {}", args.query);
    }

    let mut iso_entries: Vec<IsoEntry> = Vec::new();

    // First page
    let first: SearchResponse = get_with_retries_json(&client, SEARCH_URL, args.retries, args.backoff, |u| {
        {
            let mut qp = u.query_pairs_mut();
            qp.append_pair("q", args.query.as_str());
            qp.append_pair("rows", &args.rows.to_string());
            qp.append_pair("page", "1");
            qp.append_pair("output", "json");
            for f in &args.fields {
                qp.append_pair("fl[]", f.as_str());
            }
        }
    }).await?;

    let mut resp_obj = first.response.context("Unexpected search response structure, missing 'response'")?;
    let num_found = resp_obj.num_found.max(0) as usize;
    let mut total_pages = ((num_found + args.rows - 1).max(1)) / args.rows;
    if num_found > 0 && num_found % args.rows != 0 { total_pages += 1; }
    if let Some(maxp) = args.max_pages { total_pages = total_pages.min(maxp); }

    if args.verbosity >= 1 {
        eprintln!("numFound={}, pages={}", num_found, total_pages);
    }

    for page in 1..=total_pages {
        if page > 1 {
            thread::sleep(Duration::from_secs_f32(args.sleep));
            let data: SearchResponse = get_with_retries_json(&client, SEARCH_URL, args.retries, args.backoff, |u| {
                {
                    let mut qp = u.query_pairs_mut();
                    qp.append_pair("q", args.query.as_str());
                    qp.append_pair("rows", &args.rows.to_string());
                    qp.append_pair("page", &page.to_string());
                    qp.append_pair("output", "json");
                    for f in &args.fields { qp.append_pair("fl[]", f.as_str()); }
                }
            }).await?;
            if let Some(inner) = data.response { resp_obj = inner; }
        }

        let docs = &resp_obj.docs;
        if args.verbosity >= 2 {
            eprintln!("Processing page {} with {} docs", page, docs.len());
        }

        for item in docs {
            let identifier = item.get("identifier").and_then(|v| v.as_str()).unwrap_or("");
            if identifier.is_empty() { continue; }
            let title = item.get("title").and_then(|v| v.as_str()).unwrap_or("").to_string();

            if args.dry_run {
                println!("{} - {}", identifier, title);
                continue;
            }

            thread::sleep(Duration::from_secs_f32(args.sleep));
            let meta_url = format!("{}{}", METADATA_BASE_URL, identifier);
            let meta: Option<Value> = match get_with_retries_json_opt(&client, &meta_url, args.retries, args.backoff).await {
                Ok(v) => v,
                Err(_) => None,
            };
            if let Some(Value::Object(map)) = meta {
                if let Some(files) = map.get("files").and_then(|v| v.as_array()) {
                    for f in files {
                        if let Some(name) = f.get("name").and_then(|v| v.as_str()) {
                            let lname = name.to_lowercase();
                            if lname.ends_with(".iso") || lname.ends_with(".img") || lname.ends_with(".zip") {
                                let size = f.get("size").and_then(|v| v.as_i64()).map(|n| n.to_string());
                                iso_entries.push(IsoEntry {
                                    identifier: identifier.to_string(),
                                    title: title.clone(),
                                    file_name: name.to_string(),
                                    download_url: format!("{}/{}/{}", DOWNLOAD_BASE_URL, identifier, name),
                                    size,
                                });
                            }
                        }
                    }
                }
            }
        }
    }

    let mut file = File::create(&args.out).with_context(|| format!("Failed to create {}", &args.out))?;
    file.write_all(serde_json::to_string_pretty(&iso_entries)?.as_bytes())?;
    println!("Found {} ISO-like files. Saved to {}.", iso_entries.len(), &args.out);

    Ok(())
}

async fn get_with_retries_json<T: for<'de> serde::Deserialize<'de>, F: FnOnce(&mut reqwest::Url)>(client: &Client, base: &str, retries: usize, backoff: f32, url_mut: F) -> Result<T> {
    let mut url = reqwest::Url::parse(base)?;
    url_mut(&mut url);
    let mut attempt = 0usize;
    loop {
        attempt += 1;
        let res = client.get(url.clone()).send().await;
        match res {
            Ok(resp) => {
                if resp.status() == StatusCode::OK {
                    let v = resp.json::<T>().await?;
                    return Ok(v);
                } else if matches!(resp.status(), StatusCode::TOO_MANY_REQUESTS | StatusCode::INTERNAL_SERVER_ERROR | StatusCode::BAD_GATEWAY | StatusCode::SERVICE_UNAVAILABLE | StatusCode::GATEWAY_TIMEOUT) {
                    // retryable
                } else {
                    let status = resp.status();
                    let text = resp.text().await.unwrap_or_default();
                    anyhow::bail!("Request failed with status {}: {}", status, text);
                }
            }
            Err(_) => { /* retry */ }
        }
        if attempt > retries { anyhow::bail!("Failed after {} retries", retries); }
        let sleep = backoff * attempt as f32;
        thread::sleep(Duration::from_secs_f32(sleep));
    }
}

async fn get_with_retries_json_opt(client: &Client, url: &str, retries: usize, backoff: f32) -> Result<Option<Value>> {
    let mut attempt = 0usize;
    loop {
        attempt += 1;
        match client.get(url).send().await {
            Ok(resp) => {
                if resp.status().is_success() {
                    let v: Value = resp.json().await?;
                    return Ok(Some(v));
                } else if matches!(resp.status(), StatusCode::TOO_MANY_REQUESTS | StatusCode::INTERNAL_SERVER_ERROR | StatusCode::BAD_GATEWAY | StatusCode::SERVICE_UNAVAILABLE | StatusCode::GATEWAY_TIMEOUT) {
                    // retryable
                } else {
                    return Ok(None);
                }
            }
            Err(_) => { /* retry */ }
        }
        if attempt > retries { return Ok(None); }
        let sleep = backoff * attempt as f32;
        thread::sleep(Duration::from_secs_f32(sleep));
    }
}
