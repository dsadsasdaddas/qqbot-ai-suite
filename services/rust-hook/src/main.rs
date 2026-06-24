use std::collections::{HashMap, VecDeque};
use std::env;
use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::net::TcpStream;
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

static JOB_SEQ: AtomicU64 = AtomicU64::new(1);

#[derive(Clone)]
struct Config {
    bind_addr: String,
    runner_url: String,
    token_config_path: PathBuf,
    state_dir: PathBuf,
    jobs_dir: PathBuf,
    default_timeout_secs: u64,
}

#[derive(Clone)]
struct Job {
    job_id: String,
    status: String,
    source: String,
    user_id: String,
    group_id: String,
    command: String,
    task: String,
    engine: String,
    continue_session: bool,
    timeout_seconds: u64,
    created_at: u64,
    started_at: u64,
    finished_at: u64,
    summary: String,
    result: String,
    error: String,
    runner_rc: i64,
    has_runner_rc: bool,
    cancel_requested: bool,
}

struct Inner {
    jobs: HashMap<String, Job>,
    queue: VecDeque<String>,
}

struct AppState {
    cfg: Config,
    inner: Mutex<Inner>,
    cv: Condvar,
}

fn main() {
    let cfg = Config::from_env();
    fs::create_dir_all(&cfg.jobs_dir).ok();
    let state = Arc::new(AppState {
        cfg: cfg.clone(),
        inner: Mutex::new(Inner { jobs: HashMap::new(), queue: VecDeque::new() }),
        cv: Condvar::new(),
    });

    {
        let s = state.clone();
        thread::spawn(move || worker_loop(s));
    }

    eprintln!("qqbot-hookd listening on {} runner={} jobs_dir={}", cfg.bind_addr, cfg.runner_url, cfg.jobs_dir.display());
    let listener = TcpListener::bind(&cfg.bind_addr).expect("bind failed");
    for stream in listener.incoming() {
        match stream {
            Ok(stream) => {
                let s = state.clone();
                thread::spawn(move || handle_client(stream, s));
            }
            Err(e) => eprintln!("accept error: {e}"),
        }
    }
}

impl Config {
    fn from_env() -> Self {
        let bind_addr = env::var("BIND_ADDR").unwrap_or_else(|_| "0.0.0.0:8788".to_string());
        let runner_url = env::var("RUNNER_URL").unwrap_or_else(|_| "http://claude-glm-runner:8790".to_string());
        let token_config_path = PathBuf::from(env::var("TOKEN_CONFIG_PATH").unwrap_or_else(|_| "/config/claude_glm_runner.json".to_string()));
        let state_dir = PathBuf::from(env::var("STATE_DIR").unwrap_or_else(|_| "/data/.agent_state".to_string()));
        let jobs_dir = PathBuf::from(env::var("JOBS_DIR").unwrap_or_else(|_| state_dir.join("jobs").display().to_string()));
        let default_timeout_secs = env::var("DEFAULT_TIMEOUT_SECS").ok().and_then(|x| x.parse().ok()).unwrap_or(1200);
        Self { bind_addr, runner_url, token_config_path, state_dir, jobs_dir, default_timeout_secs }
    }
}

fn handle_client(mut stream: TcpStream, state: Arc<AppState>) {
    let _ = stream.set_read_timeout(Some(Duration::from_secs(15)));
    let mut buf = Vec::new();
    let mut tmp = [0u8; 8192];
    let mut header_end = None;
    let mut content_len = 0usize;

    loop {
        match stream.read(&mut tmp) {
            Ok(0) => break,
            Ok(n) => {
                buf.extend_from_slice(&tmp[..n]);
                if header_end.is_none() {
                    if let Some(pos) = find_bytes(&buf, b"\r\n\r\n") {
                        header_end = Some(pos + 4);
                        let headers = String::from_utf8_lossy(&buf[..pos]).to_string();
                        content_len = parse_content_len(&headers);
                    }
                }
                if let Some(end) = header_end {
                    if buf.len() >= end + content_len { break; }
                    if buf.len() > 2 * 1024 * 1024 { break; }
                }
            }
            Err(_) => break,
        }
    }

    let Some(end) = header_end else {
        write_response(&mut stream, 400, "{\"ok\":false,\"error\":\"bad request\"}");
        return;
    };
    let head = String::from_utf8_lossy(&buf[..end]).to_string();
    let body = String::from_utf8_lossy(&buf[end..]).to_string();
    let mut lines = head.lines();
    let first = lines.next().unwrap_or("");
    let mut parts = first.split_whitespace();
    let method = parts.next().unwrap_or("");
    let target = parts.next().unwrap_or("/");
    let (path, query) = split_query(target);
    let headers = parse_headers(&head);

    if path != "/health" && !authorized(&headers, &state.cfg) {
        write_response(&mut stream, 401, "{\"ok\":false,\"error\":\"unauthorized\"}");
        return;
    }

    let (code, resp) = route(method, &path, &query, &body, state);
    write_response(&mut stream, code, &resp);
}

fn route(method: &str, path: &str, query: &str, body: &str, state: Arc<AppState>) -> (u16, String) {
    if method == "GET" && path == "/health" {
        return (200, "{\"ok\":true,\"service\":\"qqbot-hookd\",\"version\":\"0.1.0-std\"}".to_string());
    }
    if path == "/status" && (method == "GET" || method == "POST") {
        return (200, status_json(&state));
    }
    if path == "/state" && method == "POST" {
        let limit = json_get_u64(body, "limit").unwrap_or(7000);
        let req = format!("{{\"limit\":{}}}", limit);
        return match post_runner(&state.cfg, "/state", &req, 60) {
            Ok(v) => (200, v),
            Err(e) => (502, json_obj_error(&e)),
        };
    }
    if path == "/jobs" && method == "POST" {
        return create_job(&state, body);
    }
    if path == "/jobs" && method == "GET" {
        let limit = query_param(query, "limit").and_then(|x| x.parse().ok()).unwrap_or(20usize).min(100);
        return (200, list_jobs_json(&state, limit));
    }
    if method == "GET" && path.starts_with("/jobs/") && path.ends_with("/log") {
        let id = path.trim_start_matches("/jobs/").trim_end_matches("/log").trim_matches('/').to_string();
        let tail = query_param(query, "tail").and_then(|x| x.parse().ok()).unwrap_or(300usize).min(5000);
        return get_job_log(&state, &id, tail);
    }
    if method == "GET" && path.starts_with("/jobs/") {
        let id = path.trim_start_matches("/jobs/").trim_matches('/').to_string();
        return get_job(&state, &id);
    }
    if method == "POST" && path.starts_with("/jobs/") && path.ends_with("/cancel") {
        let id = path.trim_start_matches("/jobs/").trim_end_matches("/cancel").trim_matches('/').to_string();
        return cancel_job(&state, &id);
    }
    (404, "{\"ok\":false,\"error\":\"not found\"}".to_string())
}

fn create_job(state: &Arc<AppState>, body: &str) -> (u16, String) {
    let task = json_get_string(body, "task").unwrap_or_default();
    if task.trim().is_empty() {
        return (400, "{\"ok\":false,\"error\":\"empty task\"}".to_string());
    }
    let id = new_job_id();
    let job = Job {
        job_id: id.clone(),
        status: "queued".to_string(),
        source: json_get_string(body, "source").unwrap_or_else(|| "qq".to_string()),
        user_id: json_get_string(body, "user_id").unwrap_or_default(),
        group_id: json_get_string(body, "group_id").unwrap_or_default(),
        command: json_get_string(body, "command").unwrap_or_else(|| "cc".to_string()),
        task: task.clone(),
        engine: json_get_string(body, "engine").unwrap_or_else(|| "claude_glm".to_string()),
        continue_session: json_get_bool(body, "continue_session").unwrap_or(true),
        timeout_seconds: json_get_u64(body, "timeout_seconds").unwrap_or(state.cfg.default_timeout_secs),
        created_at: now_secs(),
        started_at: 0,
        finished_at: 0,
        summary: "已入队，等待执行".to_string(),
        result: String::new(),
        error: String::new(),
        runner_rc: 0,
        has_runner_rc: false,
        cancel_requested: false,
    };
    if let Err(e) = create_job_files(&state.cfg.jobs_dir, &job) {
        return (500, json_obj_error(&format!("create files failed: {e}")));
    }
    {
        let mut inner = state.inner.lock().unwrap();
        inner.jobs.insert(id.clone(), job.clone());
        inner.queue.push_back(id.clone());
    }
    state.cv.notify_one();
    (200, format!("{{\"ok\":true,\"job_id\":\"{}\",\"status\":\"queued\",\"job\":{}}}", json_escape(&id), job_summary_json(&job)))
}

fn list_jobs_json(state: &Arc<AppState>, limit: usize) -> String {
    let mut jobs = {
        let inner = state.inner.lock().unwrap();
        inner.jobs.values().cloned().collect::<Vec<_>>()
    };
    jobs.sort_by(|a, b| b.created_at.cmp(&a.created_at));
    let arr = jobs.into_iter().take(limit).map(|j| job_summary_json(&j)).collect::<Vec<_>>().join(",");
    format!("{{\"ok\":true,\"jobs\":[{}]}}", arr)
}

fn get_job(state: &Arc<AppState>, id: &str) -> (u16, String) {
    let job = { state.inner.lock().unwrap().jobs.get(id).cloned() };
    match job {
        Some(j) => (200, format!("{{\"ok\":true,\"job\":{}}}", job_json(&j))),
        None => (404, "{\"ok\":false,\"error\":\"job not found\"}".to_string()),
    }
}

fn get_job_log(state: &Arc<AppState>, id: &str, tail: usize) -> (u16, String) {
    let dir = state.cfg.jobs_dir.join(id);
    if !dir.exists() { return (404, "{\"ok\":false,\"error\":\"job not found\"}".to_string()); }
    let job_json_txt = fs::read_to_string(dir.join("job.json")).unwrap_or_else(|_| "null".to_string());
    let stdout = tail_lines(&dir.join("stdout.log"), tail);
    let stderr = tail_lines(&dir.join("stderr.log"), tail);
    let result = fs::read_to_string(dir.join("result.md")).unwrap_or_default();
    (200, format!("{{\"ok\":true,\"job\":{},\"stdout\":\"{}\",\"stderr\":\"{}\",\"result\":\"{}\"}}", job_json_txt, json_escape(&stdout), json_escape(&stderr), json_escape(&result)))
}

fn cancel_job(state: &Arc<AppState>, id: &str) -> (u16, String) {
    let mut out = None;
    {
        let mut inner = state.inner.lock().unwrap();
        let mut remove_from_queue = false;
        if let Some(job) = inner.jobs.get_mut(id) {
            if job.status == "queued" {
                job.status = "cancelled".to_string();
                job.finished_at = now_secs();
                job.summary = "已取消（排队阶段）".to_string();
                remove_from_queue = true;
            } else if job.status == "running" || job.status == "waiting_input" {
                job.cancel_requested = true;
                job.summary = "已请求取消；当前版本通过 HTTP 调 runner，等待 runner 返回后收尾".to_string();
            }
            out = Some(job.clone());
        }
        if remove_from_queue {
            inner.queue.retain(|x| x != id);
        }
    }
    match out {
        Some(j) => {
            persist_job(&state.cfg.jobs_dir, &j).ok();
            (200, format!("{{\"ok\":true,\"job\":{}}}", job_json(&j)))
        }
        None => (404, "{\"ok\":false,\"error\":\"job not found\"}".to_string()),
    }
}

fn status_json(state: &Arc<AppState>) -> String {
    let (queue_len, total, current, counts) = {
        let inner = state.inner.lock().unwrap();
        let mut counts: HashMap<String, u64> = HashMap::new();
        let mut current = Vec::new();
        for j in inner.jobs.values() {
            *counts.entry(j.status.clone()).or_insert(0) += 1;
            if j.status == "running" { current.push(job_summary_json(j)); }
        }
        (inner.queue.len(), inner.jobs.len(), current.join(","), counts)
    };
    let counts_json = counts.iter().map(|(k,v)| format!("\"{}\":{}", json_escape(k), v)).collect::<Vec<_>>().join(",");
    let runner = post_runner(&state.cfg, "/status", "{}", 60).unwrap_or_else(|e| json_obj_error(&e));
    format!(
        "{{\"ok\":true,\"service\":\"qqbot-hookd\",\"version\":\"0.1.0-std\",\"runner_url\":\"{}\",\"state_dir\":\"{}\",\"jobs_dir\":\"{}\",\"queue_len\":{},\"total_jobs\":{},\"max_concurrency\":1,\"counts\":{{{}}},\"current_jobs\":[{}],\"runner\":{}}}",
        json_escape(&state.cfg.runner_url), json_escape(&state.cfg.state_dir.display().to_string()), json_escape(&state.cfg.jobs_dir.display().to_string()), queue_len, total, counts_json, current, runner
    )
}

fn worker_loop(state: Arc<AppState>) {
    loop {
        let id = {
            let mut inner = state.inner.lock().unwrap();
            while inner.queue.is_empty() {
                inner = state.cv.wait(inner).unwrap();
            }
            inner.queue.pop_front().unwrap()
        };
        run_job(&state, &id);
    }
}

fn run_job(state: &Arc<AppState>, id: &str) {
    let job = {
        let mut inner = state.inner.lock().unwrap();
        let Some(j) = inner.jobs.get_mut(id) else { return; };
        if j.status == "cancelled" { return; }
        j.status = "running".to_string();
        j.started_at = now_secs();
        j.summary = "Claude Code(GLM) 执行中".to_string();
        j.clone()
    };
    persist_job(&state.cfg.jobs_dir, &job).ok();
    append_file(&state.cfg.jobs_dir.join(id).join("stdout.log"), "[hookd] job started\n").ok();

    let payload = format!("{{\"task\":\"{}\",\"timeout\":{},\"continue_session\":{}}}", json_escape(&job.task), job.timeout_seconds, if job.continue_session { "true" } else { "false" });
    let started = now_secs();
    let res = post_runner(&state.cfg, "/run", &payload, job.timeout_seconds + 60);

    let final_job = {
        let mut inner = state.inner.lock().unwrap();
        let Some(real) = inner.jobs.get_mut(id) else { return; };
        if real.cancel_requested {
            real.status = "cancelled".to_string();
            real.finished_at = now_secs();
            real.summary = "已取消（runner 返回后收尾）".to_string();
        } else {
            match res {
                Ok(v) => {
                    let ok = json_get_bool(&v, "ok").unwrap_or(false);
                    let rc = json_get_i64(&v, "rc").unwrap_or(0);
                    real.runner_rc = rc;
                    real.has_runner_rc = json_find_key(&v, "rc").is_some();
                    real.finished_at = now_secs();
                    real.result = json_get_string(&v, "text").unwrap_or_default();
                    let stdout = json_get_string(&v, "stdout").unwrap_or_default();
                    let stderr = json_get_string(&v, "stderr").unwrap_or_default();
                    write_job_output(&state.cfg.jobs_dir, id, &real.result, &stdout, &stderr).ok();
                    if ok {
                        real.status = "done".to_string();
                        real.summary = format!("完成，用时 {} 秒", now_secs().saturating_sub(started));
                    } else {
                        real.status = "failed".to_string();
                        real.error = json_get_string(&v, "error").unwrap_or_else(|| "runner returned ok=false".to_string());
                        real.summary = "runner 执行失败".to_string();
                    }
                }
                Err(e) => {
                    real.status = "failed".to_string();
                    real.finished_at = now_secs();
                    real.error = e;
                    real.summary = "调用 runner 失败".to_string();
                }
            }
        }
        real.clone()
    };
    persist_job(&state.cfg.jobs_dir, &final_job).ok();
    eprintln!("job {} finished status={}", id, final_job.status);
}

fn post_runner(cfg: &Config, path: &str, body: &str, timeout_secs: u64) -> Result<String, String> {
    let token = read_token(&cfg.token_config_path).unwrap_or_default();
    let (host, port) = parse_http_host_port(&cfg.runner_url)?;
    let mut stream = TcpStream::connect((host.as_str(), port)).map_err(|e| format!("connect runner failed: {e}"))?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(timeout_secs.max(5))));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(20)));
    let mut req = format!(
        "POST {} HTTP/1.1\r\nHost: {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n",
        path, host, body.as_bytes().len()
    );
    if !token.is_empty() {
        req.push_str(&format!("Authorization: Bearer {}\r\n", token));
    }
    req.push_str("\r\n");
    stream.write_all(req.as_bytes()).map_err(|e| format!("write runner req failed: {e}"))?;
    stream.write_all(body.as_bytes()).map_err(|e| format!("write runner body failed: {e}"))?;
    let mut resp = Vec::new();
    stream.read_to_end(&mut resp).map_err(|e| format!("read runner resp failed: {e}"))?;
    let txt = String::from_utf8_lossy(&resp).to_string();
    let status_ok = txt.starts_with("HTTP/1.1 2") || txt.starts_with("HTTP/1.0 2");
    let body = txt.split("\r\n\r\n").nth(1).unwrap_or(&txt).to_string();
    if !status_ok {
        return Err(format!("runner http error: {}", body));
    }
    Ok(body)
}

fn parse_http_host_port(url: &str) -> Result<(String, u16), String> {
    let u = url.trim().trim_start_matches("http://").trim_start_matches("https://");
    let hostport = u.split('/').next().unwrap_or(u);
    let mut p = hostport.split(':');
    let host = p.next().unwrap_or("").to_string();
    let port = p.next().and_then(|x| x.parse().ok()).unwrap_or(80);
    if host.is_empty() { Err("bad runner url".to_string()) } else { Ok((host, port)) }
}

fn authorized(headers: &HashMap<String, String>, cfg: &Config) -> bool {
    let token = read_token(&cfg.token_config_path).unwrap_or_default();
    if token.is_empty() { return true; }
    let auth = headers.get("authorization").cloned().unwrap_or_default();
    auth.strip_prefix("Bearer ").map(|x| x == token).unwrap_or(false)
}

fn read_token(path: &Path) -> Option<String> {
    let s = fs::read_to_string(path).ok()?;
    json_get_string(&s, "token")
}

fn create_job_files(jobs_dir: &Path, job: &Job) -> std::io::Result<()> {
    let dir = jobs_dir.join(&job.job_id);
    fs::create_dir_all(&dir)?;
    fs::write(dir.join("prompt.txt"), &job.task)?;
    fs::write(dir.join("stdout.log"), "")?;
    fs::write(dir.join("stderr.log"), "")?;
    fs::write(dir.join("result.md"), "")?;
    persist_job(jobs_dir, job)
}

fn persist_job(jobs_dir: &Path, job: &Job) -> std::io::Result<()> {
    let dir = jobs_dir.join(&job.job_id);
    fs::create_dir_all(&dir)?;
    let tmp = dir.join("job.json.tmp");
    fs::write(&tmp, job_json(job))?;
    fs::rename(tmp, dir.join("job.json"))?;
    Ok(())
}

fn write_job_output(jobs_dir: &Path, id: &str, result: &str, stdout: &str, stderr: &str) -> std::io::Result<()> {
    let dir = jobs_dir.join(id);
    if !stdout.is_empty() { fs::write(dir.join("stdout.log"), stdout)?; }
    if !stderr.is_empty() { fs::write(dir.join("stderr.log"), stderr)?; }
    if !result.is_empty() { fs::write(dir.join("result.md"), result)?; }
    Ok(())
}

fn append_file(path: &Path, text: &str) -> std::io::Result<()> {
    let mut f = OpenOptions::new().create(true).append(true).open(path)?;
    f.write_all(text.as_bytes())
}

fn job_summary_json(j: &Job) -> String {
    format!(
        "{{\"job_id\":\"{}\",\"status\":\"{}\",\"engine\":\"{}\",\"user_id\":\"{}\",\"group_id\":\"{}\",\"created_at\":{},\"started_at\":{},\"finished_at\":{},\"summary\":\"{}\",\"task_preview\":\"{}\",\"has_result\":{},\"error\":\"{}\"}}",
        json_escape(&j.job_id), json_escape(&j.status), json_escape(&j.engine), json_escape(&j.user_id), json_escape(&j.group_id), j.created_at, j.started_at, j.finished_at, json_escape(&j.summary), json_escape(&preview(&j.task, 80)), if j.result.is_empty() { "false" } else { "true" }, json_escape(&preview(&j.error, 160))
    )
}

fn job_json(j: &Job) -> String {
    format!(
        "{{\"job_id\":\"{}\",\"status\":\"{}\",\"source\":\"{}\",\"user_id\":\"{}\",\"group_id\":\"{}\",\"command\":\"{}\",\"task\":\"{}\",\"engine\":\"{}\",\"continue_session\":{},\"timeout_seconds\":{},\"created_at\":{},\"started_at\":{},\"finished_at\":{},\"summary\":\"{}\",\"result\":\"{}\",\"error\":\"{}\",\"runner_rc\":{},\"cancel_requested\":{}}}",
        json_escape(&j.job_id), json_escape(&j.status), json_escape(&j.source), json_escape(&j.user_id), json_escape(&j.group_id), json_escape(&j.command), json_escape(&j.task), json_escape(&j.engine), if j.continue_session { "true" } else { "false" }, j.timeout_seconds, j.created_at, j.started_at, j.finished_at, json_escape(&j.summary), json_escape(&j.result), json_escape(&j.error), if j.has_runner_rc { j.runner_rc.to_string() } else { "null".to_string() }, if j.cancel_requested { "true" } else { "false" }
    )
}

fn json_obj_error(e: &str) -> String {
    format!("{{\"ok\":false,\"error\":\"{}\"}}", json_escape(e))
}

fn write_response(stream: &mut TcpStream, code: u16, body: &str) {
    let status = match code { 200 => "OK", 400 => "Bad Request", 401 => "Unauthorized", 404 => "Not Found", 500 => "Internal Server Error", 502 => "Bad Gateway", _ => "OK" };
    let resp = format!("HTTP/1.1 {} {}\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}", code, status, body.as_bytes().len(), body);
    let _ = stream.write_all(resp.as_bytes());
}

fn parse_headers(head: &str) -> HashMap<String, String> {
    let mut map = HashMap::new();
    for line in head.lines().skip(1) {
        if let Some((k, v)) = line.split_once(':') {
            map.insert(k.trim().to_ascii_lowercase(), v.trim().to_string());
        }
    }
    map
}

fn parse_content_len(head: &str) -> usize {
    for line in head.lines() {
        if let Some((k, v)) = line.split_once(':') {
            if k.trim().eq_ignore_ascii_case("content-length") {
                return v.trim().parse().unwrap_or(0);
            }
        }
    }
    0
}

fn split_query(target: &str) -> (String, String) {
    if let Some((p, q)) = target.split_once('?') { (p.to_string(), q.to_string()) } else { (target.to_string(), String::new()) }
}

fn query_param(query: &str, key: &str) -> Option<String> {
    for part in query.split('&') {
        if let Some((k, v)) = part.split_once('=') {
            if k == key { return Some(percent_decode(v)); }
        }
    }
    None
}

fn find_bytes(hay: &[u8], needle: &[u8]) -> Option<usize> {
    hay.windows(needle.len()).position(|w| w == needle)
}

fn tail_lines(path: &Path, n: usize) -> String {
    let s = fs::read_to_string(path).unwrap_or_default();
    let mut lines = s.lines().rev().take(n).collect::<Vec<_>>();
    lines.reverse();
    lines.join("\n")
}

fn new_job_id() -> String {
    let t = now_millis();
    let seq = JOB_SEQ.fetch_add(1, Ordering::Relaxed);
    format!("{:x}{:04x}", t, seq % 0xffff)
}

fn now_secs() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs()
}

fn now_millis() -> u128 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis()
}

fn preview(s: &str, max: usize) -> String {
    let mut out = s.chars().take(max).collect::<String>();
    if s.chars().count() > max { out.push_str("..."); }
    out
}

fn json_escape(s: &str) -> String {
    let mut out = String::new();
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c < ' ' => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out
}

fn json_find_key(s: &str, key: &str) -> Option<usize> {
    let pat = format!("\"{}\"", key);
    s.find(&pat).and_then(|i| s[i+pat.len()..].find(':').map(|j| i + pat.len() + j + 1))
}

fn json_get_string(s: &str, key: &str) -> Option<String> {
    let mut i = json_find_key(s, key)?;
    let bytes = s.as_bytes();
    while i < bytes.len() && bytes[i].is_ascii_whitespace() { i += 1; }
    if i >= bytes.len() || bytes[i] != b'"' { return None; }
    i += 1;
    let mut out = String::new();
    let mut chars = s[i..].chars();
    while let Some(c) = chars.next() {
        match c {
            '"' => return Some(out),
            '\\' => {
                let e = chars.next()?;
                match e {
                    '"' => out.push('"'),
                    '\\' => out.push('\\'),
                    '/' => out.push('/'),
                    'n' => out.push('\n'),
                    'r' => out.push('\r'),
                    't' => out.push('\t'),
                    'b' => out.push('\u{0008}'),
                    'f' => out.push('\u{000c}'),
                    'u' => {
                        let mut hex = String::new();
                        for _ in 0..4 { hex.push(chars.next()?); }
                        if let Ok(v) = u32::from_str_radix(&hex, 16) {
                            if let Some(ch) = char::from_u32(v) { out.push(ch); }
                        }
                    }
                    x => out.push(x),
                }
            }
            x => out.push(x),
        }
    }
    None
}

fn json_get_bool(s: &str, key: &str) -> Option<bool> {
    let i = json_find_key(s, key)?;
    let rest = s[i..].trim_start();
    if rest.starts_with("true") { Some(true) } else if rest.starts_with("false") { Some(false) } else { None }
}

fn json_get_u64(s: &str, key: &str) -> Option<u64> {
    json_get_number_str(s, key).and_then(|x| x.parse().ok())
}

fn json_get_i64(s: &str, key: &str) -> Option<i64> {
    json_get_number_str(s, key).and_then(|x| x.parse().ok())
}

fn json_get_number_str(s: &str, key: &str) -> Option<String> {
    let i = json_find_key(s, key)?;
    let rest = s[i..].trim_start();
    let mut out = String::new();
    for c in rest.chars() {
        if c.is_ascii_digit() || c == '-' { out.push(c); } else { break; }
    }
    if out.is_empty() { None } else { Some(out) }
}

fn percent_decode(s: &str) -> String {
    let mut out = Vec::new();
    let bytes = s.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            if let Ok(v) = u8::from_str_radix(&s[i+1..i+3], 16) {
                out.push(v);
                i += 3;
                continue;
            }
        }
        if bytes[i] == b'+' { out.push(b' '); } else { out.push(bytes[i]); }
        i += 1;
    }
    String::from_utf8_lossy(&out).to_string()
}
