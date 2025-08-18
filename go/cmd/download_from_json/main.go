package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

type Item struct {
	FileName    string  `json:"file_name"`
	DownloadURL string  `json:"download_url"`
	Title       *string `json:"title,omitempty"`
}

func main() {
	var (
		input      string
		outputDir  string
		retries    int
		timeout    int
		backoff    float64
		chunkSize  int
		resume     bool
		noProgress bool
		dryRun     bool
		maxItems   int
		includeRe  string
		excludeRe  string
		userAgent  string
		verbosity  int
	)

	flag.StringVar(&input, "input", "iso_metadata.json", "Path to JSON metadata file (list of items)")
	flag.StringVar(&input, "i", input, "Input JSON (shorthand)")
	flag.StringVar(&outputDir, "output-dir", "S:/Linux-FUCKIN-ISOs/", "Destination directory for downloads")
	flag.StringVar(&outputDir, "o", outputDir, "Destination directory (shorthand)")
	flag.IntVar(&retries, "retries", 5, "Total HTTP retries for transient errors")
	flag.IntVar(&timeout, "timeout", 60, "Request timeout seconds")
	flag.Float64Var(&backoff, "backoff", 1.0, "Retry backoff factor")
	flag.IntVar(&chunkSize, "chunk-size", 1024*256, "Download chunk size in bytes")
	flag.BoolVar(&resume, "resume", false, "Resume partially downloaded files using HTTP Range")
	flag.BoolVar(&noProgress, "no-progress", false, "Disable per-file progress output")
	flag.BoolVar(&dryRun, "dry-run", false, "Do not download, just list actions")
	flag.IntVar(&maxItems, "max", 0, "Limit the number of items to process (0=all)")
	flag.StringVar(&includeRe, "include", "", "Regex that file_name/title must match to be downloaded")
	flag.StringVar(&excludeRe, "exclude", "", "Regex that if matched will skip the item")
	flag.StringVar(&userAgent, "user-agent", "", "Custom User-Agent header")
	flag.IntVar(&verbosity, "v", 0, "Increase verbosity (-v info, -vv debug) [repeat the flag]")
	flag.Parse()

	ua := userAgent
	if ua == "" {
		ua = "Internet-Archive-API/go"
	}

	f, err := os.Open(input)
	if err != nil {
		fatal(err)
	}
	defer f.Close()
	var data []Item
	if err := json.NewDecoder(f).Decode(&data); err != nil {
		fatal(fmt.Errorf("input JSON must be a list of items: %w", err))
	}

	// Compile filters
	var inc, exc *regexp.Regexp
	if includeRe != "" {
		inc, err = regexp.Compile("(?i)" + includeRe)
		if err != nil {
			fatal(err)
		}
	}
	if excludeRe != "" {
		exc, err = regexp.Compile("(?i)" + excludeRe)
		if err != nil {
			fatal(err)
		}
	}

	os.MkdirAll(outputDir, 0o755)

	client := &http.Client{Timeout: time.Duration(timeout) * time.Second}

	// Filter data
	items := make([]Item, 0, len(data))
	for _, it := range data {
		name := strings.TrimSpace(it.FileName + " " + deref(it.Title))
		if inc != nil && !inc.MatchString(name) {
			continue
		}
		if exc != nil && exc.MatchString(name) {
			continue
		}
		items = append(items, it)
		if maxItems > 0 && len(items) >= maxItems {
			break
		}
	}

	total := len(items)
	log := func(level int, format string, a ...any) {
		if verbosity >= level {
			fmt.Fprintf(os.Stderr, format+"\n", a...)
		}
	}
	log(1, "Total items to process: %d", total)

	success, skipped, failed := 0, 0, 0
	for idx, it := range items {
		if it.FileName == "" || it.DownloadURL == "" {
			failed++
			continue
		}
		destPath := filepath.Join(outputDir, filepath.Clean(it.FileName))
		prefix := fmt.Sprintf("[%d/%d %.1f%%]", idx+1, total, float64(idx+1)/float64(total)*100.0)

		if _, err := os.Stat(destPath); err == nil {
			log(1, "%s Already exists: %s", prefix, it.FileName)
			skipped++
			continue
		}

		if dryRun {
			fmt.Printf("%s [DRY-RUN] Would download: %s <- %s\n", prefix, it.FileName, it.DownloadURL)
			skipped++
			continue
		}

		if err := downloadWithRetries(client, it.DownloadURL, destPath, chunkSize, retries, backoff, resume, !noProgress, ua, prefix); err != nil {
			fmt.Fprintln(os.Stderr, prefix, "[✗] Failed:", it.FileName, "-", err)
			failed++
		} else {
			fmt.Println(prefix, "[✔] Done:", it.FileName)
			success++
		}
	}
	fmt.Fprintf(os.Stderr, "Completed. Success: %d, Skipped: %d, Failed: %d\n", success, skipped, failed)
}

func downloadWithRetries(client *http.Client, url, dest string, chunkSize int, retries int, backoff float64, resume bool, showProgress bool, ua string, prefix string) error {
	for attempt := 1; attempt <= retries; attempt++ {
		if err := downloadOnce(client, url, dest, chunkSize, resume, showProgress, ua, prefix); err != nil {
			if attempt >= retries {
				return err
			}
			time.Sleep(time.Duration(float64(time.Second) * backoff * float64(attempt)))
			continue
		}
		return nil
	}
	return fmt.Errorf("failed after %d retries", retries)
}

func downloadOnce(client *http.Client, url, dest string, chunkSize int, resume bool, showProgress bool, ua string, prefix string) error {
	var downloaded int64 = 0
	mode := os.O_CREATE | os.O_WRONLY | os.O_TRUNC
	req, _ := http.NewRequest("GET", url, nil)
	req.Header.Set("User-Agent", ua)
	if resume {
		if fi, err := os.Stat(dest); err == nil {
			downloaded = fi.Size()
			if downloaded > 0 {
				req.Header.Set("Range", fmt.Sprintf("bytes=%d-", downloaded))
				mode = os.O_CREATE | os.O_WRONLY | os.O_APPEND
			}
		}
	}

	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if !(resp.StatusCode == 200 || resp.StatusCode == 206) {
		io.Copy(io.Discard, resp.Body)
		return fmt.Errorf("HTTP %s", resp.Status)
	}

	// Determine total size
	var total int64 = 0
	if cl := resp.Header.Get("Content-Length"); cl != "" {
		var n int64
		fmt.Sscanf(cl, "%d", &n)
		if resp.StatusCode == 206 {
			total = n + downloaded
		} else {
			total = n
		}
	}

	f, err := os.OpenFile(dest, mode, 0o644)
	if err != nil {
		return err
	}
	defer f.Close()

	reader := bufio.NewReader(resp.Body)
	buf := make([]byte, chunkSize)
	last := time.Now()
	for {
		n, er := reader.Read(buf)
		if n > 0 {
			if _, werr := f.Write(buf[:n]); werr != nil {
				return werr
			}
			downloaded += int64(n)
			if showProgress {
				if time.Since(last) > 50*time.Millisecond {
					printBar(prefix, dest, downloaded, total)
					last = time.Now()
				}
			}
		}
		if er != nil {
			if er == io.EOF {
				break
			}
			return er
		}
	}
	if showProgress {
		printBar(prefix, dest, downloaded, total)
		fmt.Println()
	}
	return nil
}

func printBar(prefix, dest string, downloaded, total int64) {
	name := filepath.Base(dest)
	barWidth := 40
	if total > 0 {
		frac := float64(downloaded) / float64(total)
		if frac > 1 {
			frac = 1
		}
		filled := int(frac * float64(barWidth))
		bar := strings.Repeat("#", filled) + strings.Repeat("-", barWidth-filled)
		fmt.Printf("\r%s [%s] %3.0f%% (%s/%s)", prefix, bar, frac*100, human(downloaded), human(total))
	} else {
		hashes := int(downloaded / (10 * 1024 * 1024))
		if hashes > barWidth {
			hashes = barWidth
		}
		bar := strings.Repeat("#", hashes)
		fmt.Printf("\r%s [%-*s] %s", prefix, barWidth, bar, name)
	}
}

func human(n int64) string {
	units := []string{"B", "KB", "MB", "GB", "TB"}
	f := float64(n)
	for i := 0; i < len(units); i++ {
		if f < 1024 || i == len(units)-1 {
			return fmt.Sprintf("%.1f%s", f, units[i])
		}
		f /= 1024
	}
	return fmt.Sprintf("%dB", n)
}

func deref(p *string) string {
	if p == nil {
		return ""
	}
	return *p
}

func fatal(err error) { fmt.Fprintln(os.Stderr, "Error:", err); os.Exit(1) }
