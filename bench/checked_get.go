// checked_get measures one GET route with concurrent keep-alive clients.
// Each response must match the expected status, headers, and body checks.
package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"sort"
	"strings"
	"sync"
	"time"
)

type workerResult struct {
	latencies []float64
	errors    int
	finished  time.Time
}

type target struct {
	Path   string `json:"path"`
	Cookie string `json:"cookie"`
}

type report struct {
	Clients   int     `json:"clients"`
	Seconds   float64 `json:"seconds"`
	Successes int     `json:"successes"`
	Errors    int     `json:"errors"`
	RPS       float64 `json:"rps"`
	MedianMS  float64 `json:"median_ms"`
	P95MS     float64 `json:"p95_ms"`
	P99MS     float64 `json:"p99_ms"`
}

func main() {
	base := flag.String("base", "", "server origin")
	path := flag.String("path", "", "GET path and query")
	cookie := flag.String("cookie", "", "session cookie")
	targetsFile := flag.String("targets-file", "", "JSON array of path and cookie pairs, assigned to clients round-robin")
	expectedHex := flag.String("expected-sha256", "", "SHA-256 of the expected response body")
	expectedStatus := flag.Int("expected-status", http.StatusOK, "expected HTTP status")
	expectedETag := flag.String("expected-etag", "", "expected ETag response header")
	expectedModified := flag.String("expected-last-modified", "", "expected Last-Modified response header")
	expectedContentType := flag.String("expected-content-type", "", "expected response Content-Type media type")
	expectedMessages := flag.Int("expected-message-count", 0, "expected number of rendered message IDs")
	expectedCSRF := flag.Int("expected-csrf-count", 0, "expected number of hidden authenticity_token fields")
	ifNoneMatch := flag.String("if-none-match", "", "conditional request ETag")
	signalStart := flag.Bool("signal-start", false, "write MEASURE_START to stderr after all client warmups")
	invalidSample := flag.String("invalid-sample", "", "save the first invalid response body for diagnosis")
	accept := flag.String("accept", "application/json", "Accept request header")
	clients := flag.Int("clients", 32, "number of concurrent keep-alive clients")
	seconds := flag.Float64("seconds", 15, "measured duration")
	flag.Parse()
	if *base == "" || *clients < 1 || *seconds <= 0 || (*targetsFile == "" && (*path == "" || *cookie == "")) {
		fmt.Fprintln(os.Stderr, "base, a path/cookie or targets file, positive clients and seconds are required")
		os.Exit(2)
	}
	targets := []target{{Path: *path, Cookie: *cookie}}
	if *targetsFile != "" {
		data, err := os.ReadFile(*targetsFile)
		if err != nil || json.Unmarshal(data, &targets) != nil || len(targets) == 0 {
			fmt.Fprintln(os.Stderr, "targets-file must contain a nonempty JSON array")
			os.Exit(2)
		}
		for _, item := range targets {
			if !strings.HasPrefix(item.Path, "/") || item.Cookie == "" {
				fmt.Fprintln(os.Stderr, "each target needs an absolute path and cookie")
				os.Exit(2)
			}
		}
	}
	if *expectedMessages < 0 || *expectedCSRF < 0 || (*expectedHex == "" && *expectedMessages == 0 && *expectedCSRF == 0) {
		fmt.Fprintln(os.Stderr, "provide a body hash or a positive expected content count")
		os.Exit(2)
	}
	var expected []byte
	if *expectedHex != "" {
		var err error
		expected, err = hex.DecodeString(*expectedHex)
		if err != nil || len(expected) != sha256.Size {
			fmt.Fprintln(os.Stderr, "expected-sha256 must be a SHA-256 hex digest")
			os.Exit(2)
		}
	}
	transport := &http.Transport{
		MaxIdleConns:        *clients * 2,
		MaxIdleConnsPerHost: *clients * 2,
		MaxConnsPerHost:     *clients * 2,
		IdleConnTimeout:     30 * time.Second,
	}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, Timeout: 30 * time.Second}
	var invalidOnce sync.Once
	get := func(item target) (bool, error) {
		request, err := http.NewRequest("GET", *base+item.Path, nil)
		if err != nil {
			return false, err
		}
		request.Header.Set("Cookie", item.Cookie)
		request.Header.Set("Accept", *accept)
		if *ifNoneMatch != "" {
			request.Header.Set("If-None-Match", *ifNoneMatch)
		}
		response, err := client.Do(request)
		if err != nil {
			return false, err
		}
		body, err := io.ReadAll(response.Body)
		response.Body.Close()
		if err != nil {
			return false, err
		}
		valid := response.StatusCode == *expectedStatus
		if expected != nil {
			digest := sha256.Sum256(body)
			valid = valid && bytes.Equal(digest[:], expected)
		}
		if *expectedMessages > 0 {
			valid = valid && bytes.Count(body, []byte("data-message-id=")) == *expectedMessages
		}
		if *expectedCSRF > 0 {
			valid = valid && bytes.Count(body, []byte("name=\"authenticity_token\""))+bytes.Count(body, []byte("name='authenticity_token'")) == *expectedCSRF
		}
		if *expectedETag != "" {
			valid = valid && response.Header.Get("ETag") == *expectedETag
		}
		if *expectedModified != "" {
			valid = valid && response.Header.Get("Last-Modified") == *expectedModified
		}
		if *expectedContentType != "" {
			valid = valid && strings.SplitN(response.Header.Get("Content-Type"), ";", 2)[0] == *expectedContentType
		}
		if !valid && *invalidSample != "" {
			invalidOnce.Do(func() {
				_ = os.WriteFile(*invalidSample, body, 0600)
				fmt.Fprintf(os.Stderr, "INVALID status=%d bytes=%d content_type=%q messages=%d csrf=%d etag=%q\n", response.StatusCode, len(body), response.Header.Get("Content-Type"), bytes.Count(body, []byte("data-message-id=")), bytes.Count(body, []byte("name=\"authenticity_token\""))+bytes.Count(body, []byte("name='authenticity_token'")), response.Header.Get("ETag"))
			})
		}
		return valid, nil
	}
	ready := make(chan struct{}, *clients)
	start := make(chan struct{})
	var startAt time.Time
	results := make(chan workerResult, *clients)
	var workers sync.WaitGroup
	for i := 0; i < *clients; i++ {
		item := targets[i%len(targets)]
		workers.Add(1)
		go func() {
			defer workers.Done()
			for warmup := 0; warmup < 2; warmup++ {
				valid, err := get(item)
				if err != nil || !valid {
					results <- workerResult{errors: 1, finished: time.Now()}
					return
				}
			}
			ready <- struct{}{}
			<-start
			deadline := startAt.Add(time.Duration(*seconds * float64(time.Second)))
			result := workerResult{}
			for time.Now().Before(deadline) {
				begin := time.Now()
				valid, err := get(item)
				if err != nil || !valid {
					result.errors++
				} else {
					result.latencies = append(result.latencies, float64(time.Since(begin))/float64(time.Millisecond))
				}
			}
			result.finished = time.Now()
			results <- result
		}()
	}
	for i := 0; i < *clients; i++ {
		select {
		case <-ready:
		case result := <-results:
			fmt.Fprintf(os.Stderr, "client warmup failed: %d errors\n", result.errors)
			os.Exit(1)
		case <-time.After(30 * time.Second):
			fmt.Fprintln(os.Stderr, "client warmup timed out")
			os.Exit(1)
		}
	}
	started := time.Now()
	startAt = started
	if *signalStart {
		fmt.Fprintln(os.Stderr, "MEASURE_START")
	}
	close(start)
	workers.Wait()
	close(results)
	latencies := make([]float64, 0)
	errors := 0
	finished := started
	for result := range results {
		latencies = append(latencies, result.latencies...)
		errors += result.errors
		if result.finished.After(finished) {
			finished = result.finished
		}
	}
	if len(latencies) == 0 {
		fmt.Fprintln(os.Stderr, "no successful measured requests")
		os.Exit(1)
	}
	sort.Float64s(latencies)
	percentile := func(fraction float64) float64 {
		index := int(fraction*float64(len(latencies)-1) + 0.999999)
		return latencies[index]
	}
	elapsed := finished.Sub(started).Seconds()
	encoded, err := json.Marshal(report{
		Clients: *clients, Seconds: elapsed, Successes: len(latencies), Errors: errors,
		RPS:      float64(len(latencies)) / elapsed,
		MedianMS: percentile(.5), P95MS: percentile(.95), P99MS: percentile(.99),
	})
	if err != nil {
		panic(err)
	}
	fmt.Println(string(encoded))
	if errors > 0 {
		os.Exit(1)
	}
}
