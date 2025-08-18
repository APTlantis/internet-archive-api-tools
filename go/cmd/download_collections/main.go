package main

import (
	"bufio"
	"crypto/md5"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

type MetaFile struct {
	Name string `json:"name"`
	MD5  string `json:"md5,omitempty"`
	Size int64  `json:"size,omitempty"`
}

type MetaResponse struct {
	Files []MetaFile `json:"files"`
}

func main() {
	var (
		identifier     string
		destdir        string
		ignoreExisting bool
		checksum       bool
		retries        int
		globPat        string
		dryRun         bool
		verbosity      int
	)

	flag.StringVar(&identifier, "identifier", "", "Archive.org item identifier")
	flag.StringVar(&destdir, "destdir", "S:/Linux-FUCKIN-ISOs", "Destination directory")
	flag.BoolVar(&ignoreExisting, "ignore-existing", true, "Skip files that already exist (default: true)")
	flag.BoolVar(&checksum, "checksum", false, "Verify checksums after download if available")
	flag.IntVar(&retries, "retries", 5, "Number of retries")
	flag.StringVar(&globPat, "glob", "", "Only download files matching this glob pattern (e.g. *.iso)")
	flag.BoolVar(&dryRun, "dry-run", false, "List files without downloading")
	flag.IntVar(&verbosity, "v", 0, "Increase verbosity (-v info, -vv debug) [repeat the flag]")
	flag.Parse()

	if identifier == "" {
		if flag.NArg() > 0 {
			identifier = flag.Arg(0)
		}
		if identifier == "" {
			fatal(fmt.Errorf("identifier is required (pass --identifier or positional)"))
		}
	}

	ua := "Internet-Archive-API/go"
	client := &http.Client{Timeout: 60 * time.Second}

	os.MkdirAll(destdir, 0o755)

	// Fetch metadata
	metaURL := "https://archive.org/metadata/" + identifier
	req, _ := http.NewRequest("GET", metaURL, nil)
	req.Header.Set("User-Agent", ua)
	resp, err := client.Do(req)
	if err != nil {
		fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		fatal(fmt.Errorf("metadata HTTP %s", resp.Status))
	}
	var meta MetaResponse
	if err := json.NewDecoder(resp.Body).Decode(&meta); err != nil {
		fatal(err)
	}

	// Iterate files
	count := 0
	for _, f := range meta.Files {
		name := f.Name
		if name == "" {
			continue
		}
		if globPat != "" {
			if ok, _ := filepath.Match(globPat, name); !ok {
				continue
			}
		}
		count++
		if dryRun {
			fmt.Println(name)
			continue
		}
		dst := filepath.Join(destdir, filepath.Clean(name))
		if ignoreExisting {
			if _, err := os.Stat(dst); err == nil {
				if verbosity >= 1 {
					fmt.Fprintln(os.Stderr, "Skip existing:", name)
				}
				continue
			}
		}
		if err := downloadFile(client, identifier, name, dst, retries, ua, verbosity); err != nil {
			fmt.Fprintln(os.Stderr, "[✗]", name, "-", err)
			continue
		}
		if checksum && f.MD5 != "" {
			if ok, err := verifyMD5(dst, f.MD5); err != nil {
				fmt.Fprintln(os.Stderr, "[!] checksum error:", err)
			} else if !ok {
				fmt.Fprintln(os.Stderr, "[✗] checksum mismatch:", name)
			} else if verbosity >= 1 {
				fmt.Fprintln(os.Stderr, "[✔] checksum ok:", name)
			}
		}
		fmt.Println("[✔]", name)
	}
	if dryRun && verbosity >= 1 {
		fmt.Fprintf(os.Stderr, "Total files listed: %d\n", count)
	}
}

func downloadFile(client *http.Client, identifier, name, dest string, retries int, ua string, verbosity int) error {
	url := fmt.Sprintf("https://archive.org/download/%s/%s", identifier, name)
	for attempt := 1; attempt <= retries; attempt++ {
		req, _ := http.NewRequest("GET", url, nil)
		req.Header.Set("User-Agent", ua)
		resp, err := client.Do(req)
		if err == nil && resp.StatusCode == 200 {
			defer resp.Body.Close()
			if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
				return err
			}
			out, err := os.Create(dest)
			if err != nil {
				return err
			}
			_, copyErr := io.Copy(out, bufio.NewReader(resp.Body))
			cerr := out.Close()
			if copyErr != nil {
				return copyErr
			}
			if cerr != nil {
				return cerr
			}
			return nil
		}
		if err == nil {
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
			if !(resp.StatusCode == 429 || (resp.StatusCode >= 500 && resp.StatusCode <= 599)) {
				return fmt.Errorf("HTTP %s", resp.Status)
			}
		}
		time.Sleep(time.Duration(attempt) * time.Second)
		if verbosity >= 2 {
			fmt.Fprintln(os.Stderr, "retry", attempt, name)
		}
	}
	return fmt.Errorf("failed after %d retries", retries)
}

func verifyMD5(path string, expected string) (bool, error) {
	f, err := os.Open(path)
	if err != nil {
		return false, err
	}
	defer f.Close()
	h := md5.New()
	if _, err := io.Copy(h, f); err != nil {
		return false, err
	}
	sum := h.Sum(nil)
	return strings.EqualFold(hex.EncodeToString(sum), expected), nil
}

func fatal(err error) { fmt.Fprintln(os.Stderr, "Error:", err); os.Exit(1) }
