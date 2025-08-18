package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
)

type SearchResponse struct {
	Response *struct {
		NumFound int                      `json:"numFound"`
		Docs     []map[string]interface{} `json:"docs"`
	} `json:"response"`
	Error any `json:"error"`
}

type IsoEntry struct {
	Identifier  string  `json:"identifier"`
	Title       string  `json:"title"`
	FileName    string  `json:"file_name"`
	DownloadURL string  `json:"download_url"`
	Size        *string `json:"size,omitempty"`
}

const (
	searchURL       = "https://archive.org/advancedsearch.php"
	metadataBaseURL = "https://archive.org/metadata/"
	downloadBaseURL = "https://archive.org/download"
)

func main() {
	var (
		query     string
		rows      int
		maxPages  int
		sleepSec  float64
		fieldsStr string
		outFile   string
		timeout   int
		retries   int
		backoff   float64
		userAgent string
		verbosity int
		dryRun    bool
	)

	flag.StringVar(&query, "query", "(format:ISO OR format:IMG) AND mediatype:software AND description:\"linux, distribution\"", "Advanced search query string")
	flag.StringVar(&query, "q", query, "Advanced search query string (shorthand)")
	flag.IntVar(&rows, "rows", 500, "Rows per page (<=1000)")
	flag.IntVar(&maxPages, "max-pages", 0, "Limit number of pages to fetch (0=all)")
	flag.Float64Var(&sleepSec, "sleep", 1.0, "Sleep seconds between requests")
	flag.StringVar(&fieldsStr, "fields", "identifier title date creator", "Space-separated fields to fetch in search results")
	flag.StringVar(&outFile, "out", "iso_metadata.json", "Output JSON file for results")
	flag.StringVar(&outFile, "o", outFile, "Output JSON file (shorthand)")
	flag.IntVar(&timeout, "timeout", 30, "Request timeout seconds")
	flag.IntVar(&retries, "retries", 5, "HTTP retries for transient errors")
	flag.Float64Var(&backoff, "backoff", 1.0, "Retry backoff factor")
	flag.StringVar(&userAgent, "user-agent", "", "Custom User-Agent header")
	flag.IntVar(&verbosity, "v", 0, "Increase verbosity (-v info, -vv debug) [repeat the flag]")
	flag.BoolVar(&dryRun, "dry-run", false, "Do not fetch per-item metadata, only list identifiers")
	flag.Parse()

	fields := strings.Fields(fieldsStr)

	client := &http.Client{Timeout: time.Duration(timeout) * time.Second}
	ua := userAgent
	if ua == "" {
		ua = "Internet-Archive-API/go"
	}

	log := func(level int, format string, a ...any) {
		if verbosity >= level {
			fmt.Fprintf(os.Stderr, format+"\n", a...)
		}
	}

	log(1, "Query: %s", query)

	// Fetch first page
	first := SearchResponse{}
	if err := getJSONWithRetries(client, searchURL, retries, backoff, ua, func(u *url.URL) {
		q := u.Query()
		q.Set("q", query)
		q.Set("rows", fmt.Sprintf("%d", rows))
		q.Set("page", "1")
		q.Set("output", "json")
		for _, f := range fields {
			q.Add("fl[]", f)
		}
		u.RawQuery = q.Encode()
	}, &first); err != nil {
		fatal(err)
	}

	if first.Response == nil {
		fatal(errors.New("Unexpected search response structure, missing 'response'"))
	}

	numFound := first.Response.NumFound
	totalPages := (numFound + rows - 1) / rows
	if totalPages == 0 {
		totalPages = 1
	}
	if maxPages > 0 && totalPages > maxPages {
		totalPages = maxPages
	}
	log(1, "numFound=%d, pages=%d", numFound, totalPages)

	isoEntries := make([]IsoEntry, 0, 1024)
	respObj := first

	for page := 1; page <= totalPages; page++ {
		if page > 1 {
			time.Sleep(time.Duration(float64(time.Second) * sleepSec))
			data := SearchResponse{}
			if err := getJSONWithRetries(client, searchURL, retries, backoff, ua, func(u *url.URL) {
				q := u.Query()
				q.Set("q", query)
				q.Set("rows", fmt.Sprintf("%d", rows))
				q.Set("page", fmt.Sprintf("%d", page))
				q.Set("output", "json")
				for _, f := range fields {
					q.Add("fl[]", f)
				}
				u.RawQuery = q.Encode()
			}, &data); err != nil {
				fatal(err)
			}
			respObj = data
		}

		docs := []map[string]interface{}{}
		if respObj.Response != nil {
			docs = respObj.Response.Docs
		}
		if verbosity >= 2 {
			log(2, "Processing page %d with %d docs", page, len(docs))
		}

		for _, item := range docs {
			identifier, _ := item["identifier"].(string)
			if identifier == "" {
				continue
			}
			title, _ := item["title"].(string)

			if dryRun {
				fmt.Printf("%s - %s\n", identifier, title)
				continue
			}

			time.Sleep(time.Duration(float64(time.Second) * sleepSec))
			metaURL := metadataBaseURL + identifier
			var meta map[string]any
			if err := getJSONWithRetriesOptional(client, metaURL, retries, backoff, ua, &meta); err != nil {
				// skip on error
				continue
			}
			if filesRaw, ok := meta["files"].([]any); ok {
				for _, fr := range filesRaw {
					if f, ok := fr.(map[string]any); ok {
						name, _ := f["name"].(string)
						lname := strings.ToLower(name)
						if strings.HasSuffix(lname, ".iso") || strings.HasSuffix(lname, ".img") || strings.HasSuffix(lname, ".zip") {
							var sizePtr *string
							if sz, ok := f["size"].(string); ok && sz != "" {
								sizePtr = &sz
							}
							if szf, ok := f["size"].(float64); ok {
								s := fmt.Sprintf("%0.0f", szf)
								sizePtr = &s
							}
							isoEntries = append(isoEntries, IsoEntry{
								Identifier:  identifier,
								Title:       title,
								FileName:    name,
								DownloadURL: fmt.Sprintf("%s/%s/%s", downloadBaseURL, identifier, name),
								Size:        sizePtr,
							})
						}
					}
				}
			}
		}
	}

	f, err := os.Create(outFile)
	if err != nil {
		fatal(err)
	}
	enc := json.NewEncoder(f)
	enc.SetIndent("", "  ")
	if err := enc.Encode(isoEntries); err != nil {
		fatal(err)
	}
	fmt.Printf("Found %d ISO-like files. Saved to %s.\n", len(isoEntries), outFile)
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "Error:", err)
	os.Exit(1)
}

func getJSONWithRetries(client *http.Client, base string, retries int, backoff float64, ua string, mutate func(*url.URL), out any) error {
	u, _ := url.Parse(base)
	mutate(u)
	for attempt := 1; attempt <= retries; attempt++ {
		req, _ := http.NewRequest("GET", u.String(), nil)
		req.Header.Set("User-Agent", ua)
		resp, err := client.Do(req)
		if err == nil && resp.StatusCode == 200 {
			defer resp.Body.Close()
			dec := json.NewDecoder(resp.Body)
			return dec.Decode(out)
		}
		if err == nil {
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
			// retry on 429, 5xx
			if !(resp.StatusCode == 429 || (resp.StatusCode >= 500 && resp.StatusCode <= 599)) {
				return fmt.Errorf("request failed: %s", resp.Status)
			}
		}
		delay := time.Duration(float64(time.Second) * backoff * float64(attempt))
		time.Sleep(delay)
	}
	return fmt.Errorf("failed after %d retries", retries)
}

func getJSONWithRetriesOptional(client *http.Client, urlStr string, retries int, backoff float64, ua string, out any) error {
	for attempt := 1; attempt <= retries; attempt++ {
		req, _ := http.NewRequest("GET", urlStr, nil)
		req.Header.Set("User-Agent", ua)
		resp, err := client.Do(req)
		if err == nil && resp.StatusCode >= 200 && resp.StatusCode < 300 {
			defer resp.Body.Close()
			dec := json.NewDecoder(resp.Body)
			return dec.Decode(out)
		}
		if err == nil {
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
			if !(resp.StatusCode == 429 || (resp.StatusCode >= 500 && resp.StatusCode <= 599)) {
				return fmt.Errorf("status %s", resp.Status)
			}
		}
		time.Sleep(time.Duration(float64(time.Second) * backoff * float64(attempt)))
	}
	return fmt.Errorf("failed after %d retries", retries)
}
