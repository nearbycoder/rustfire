// A comparable HTTP load probe for Rustfire and Campfire.
// Usage: go run bench/http.go -base http://127.0.0.1:3000 -cookie 'session_token=...' -room 1 -mode read
package main

import (
	"bytes"
	"flag"
	"fmt"
	"io"
	"net/http"
	neturl "net/url"
	"os"
	"regexp"
	"sort"
	"sync"
	"sync/atomic"
	"time"
)

type result struct {
	duration time.Duration
	status   int
}

func main() {
	base := flag.String("base", "http://127.0.0.1:3000", "base URL")
	cookie := flag.String("cookie", "", "session cookie, including name")
	room := flag.Int("room", 1, "room id")
	mode := flag.String("mode", "read", "read or write")
	clients := flag.Int("clients", 32, "concurrent clients")
	seconds := flag.Int("seconds", 15, "duration")
	csrf := flag.String("csrf", "", "CSRF token for Campfire write workload")
	rich := flag.Bool("rich", false, "send Trix HTML and message[format]=html for Rustfire writes")
	flag.Parse()
	if *cookie == "" {
		fmt.Fprintln(os.Stderr, "-cookie is required")
		os.Exit(2)
	}
	if *mode != "read" && *mode != "write" {
		fmt.Fprintln(os.Stderr, "-mode must be read or write")
		os.Exit(2)
	}
	transport := &http.Transport{MaxIdleConns: *clients * 2, MaxIdleConnsPerHost: *clients * 2, MaxConnsPerHost: *clients * 2, IdleConnTimeout: 30 * time.Second}
	client := &http.Client{Transport: transport, Timeout: 10 * time.Second, CheckRedirect: func(req *http.Request, via []*http.Request) error { return http.ErrUseLastResponse }}
	if *mode == "write" && *csrf == "" {
		roomURL := fmt.Sprintf("%s/rooms/%d", *base, *room)
		req, _ := http.NewRequest("GET", roomURL, nil)
		req.Header.Set("Cookie", *cookie)
		res, err := client.Do(req)
		if err != nil || res.StatusCode != 200 {
			fmt.Fprintln(os.Stderr, "could not load room CSRF token")
			os.Exit(1)
		}
		page, _ := io.ReadAll(res.Body)
		res.Body.Close()
		match := regexp.MustCompile(`<meta[^>]*name=['"]csrf-token['"][^>]*content=['"]([^'"]+)`).FindSubmatch(page)
		if len(match) != 2 {
			fmt.Fprintln(os.Stderr, "room CSRF token was not found")
			os.Exit(1)
		}
		*csrf = string(match[1])
	}
	stop := time.Now().Add(time.Duration(*seconds) * time.Second)
	results := make(chan result, 4096)
	times := make([]time.Duration, 0)
	ok := 0
	fail := 0
	statuses := map[int]int{}
	collected := make(chan struct{})
	go func() {
		for r := range results {
			times = append(times, r.duration)
			statuses[r.status]++
			if r.status >= 200 && r.status < 300 {
				ok++
			} else {
				fail++
			}
		}
		close(collected)
	}()
	var wg sync.WaitGroup
	var seq uint64
	started := time.Now()
	for i := 0; i < *clients; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for time.Now().Before(stop) {
				var body io.Reader
				url := fmt.Sprintf("%s/rooms/%d/messages", *base, *room)
				method := "GET"
				if *mode == "write" {
					method = "POST"
					id := atomic.AddUint64(&seq, 1)
					text := fmt.Sprintf("benchmark %d", id)
					values := neturl.Values{"message[body]": {text}, "message[client_message_id]": {fmt.Sprintf("bench-%d", id)}, "authenticity_token": {*csrf}}
					if *rich {
						values.Set("message[body]", "<div>"+text+"</div>")
						values.Set("message[format]", "html")
					}
					body = bytes.NewBufferString(values.Encode())
				}
				req, _ := http.NewRequest(method, url, body)
				req.Header.Set("Cookie", *cookie)
				if *mode == "write" {
					req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
					req.Header.Set("Accept", "text/vnd.turbo-stream.html, text/html, application/xhtml+xml")
				}
				t := time.Now()
				res, err := client.Do(req)
				r := result{}
				if err == nil {
					r.status = res.StatusCode
					io.Copy(io.Discard, res.Body)
					res.Body.Close()
				}
				r.duration = time.Since(t)
				results <- r
			}
		}()
	}
	wg.Wait()
	close(results)
	<-collected
	elapsed := time.Since(started).Seconds()
	sort.Slice(times, func(i, j int) bool { return times[i] < times[j] })
	percentile := func(p float64) time.Duration {
		if len(times) == 0 {
			return 0
		}
		idx := int(p * float64(len(times)-1))
		return times[idx]
	}
	fmt.Printf("mode=%s rich=%t clients=%d seconds=%.2f requests=%d success=%d errors=%d req_per_sec=%.1f p50=%s p95=%s p99=%s statuses=%v\n", *mode, *rich, *clients, elapsed, len(times), ok, fail, float64(ok)/elapsed, percentile(.50), percentile(.95), percentile(.99), statuses)
	if fail > 0 {
		os.Exit(1)
	}
}
