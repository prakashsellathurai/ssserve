#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <sys/epoll.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/sendfile.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <netinet/tcp.h>
#include <pthread.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <string.h>
#include <signal.h>
#include <stdlib.h>
#include <stdio.h>
#include <limits.h>
#include <time.h>
#include <zlib.h>
#include <dirent.h>
#include <fnmatch.h>

#define MAX_EVENTS 64
#define MAX_CONNECTIONS 1024
#define BUFFER_SIZE 8192
#define MAX_PATH 2048
#define SENDFILE_THRESHOLD 65536
#define DEFAULT_NUM_WORKERS 4

typedef struct {
    char *pattern;
    char **lines;
    int count;
} HeaderRule;

typedef struct {
    int epoll_fd;
    int listen_fd;
    char root_dir[PATH_MAX];
    int num_workers;
    pthread_t *workers;
    volatile int running;
    PyObject *config_callback;
    int etag;
    int cors;
    int caching;
    int no_compression;
    int clean_urls;
    int trailing_slash;  /* -1=none, 0=off, 1=on */
    int single;          /* SPA mode: serve index.html for 404 */
    int symlinks;
    HeaderRule *header_rules;
    int num_header_rules;
    PyObject *live_reload;  /* LiveReload object, or NULL */
} ServerConfig;

typedef struct {
    int fd;
    char buffer[BUFFER_SIZE];
    int buf_len;
    int method;
    char path[MAX_PATH];
    char *host;
    char *accept_encoding;
    char *if_none_match;
    char *if_modified_since;
    char *range_header;
    char *origin;
} ConnectionState;

static ServerConfig server_config;

/* ================================================================
 *  ETag cache — fixed-size circular buffer, O(n) lookup
 * ================================================================ */
#define ETAG_CACHE_SIZE 2048

typedef struct {
    char key[128];
    char etag[64];
    int valid;
} ETagCacheEntry;

static ETagCacheEntry etag_cache[ETAG_CACHE_SIZE];
static int etag_cache_clock = 0;

static void etag_cache_get(long mtime, long size, char *buf, size_t buf_size) {
    char key[128];
    int klen = snprintf(key, sizeof(key), "%ld:%ld", mtime, size);
    for (int i = 0; i < ETAG_CACHE_SIZE; i++) {
        if (etag_cache[i].valid && strncmp(etag_cache[i].key, key, klen) == 0) {
            strncpy(buf, etag_cache[i].etag, buf_size);
            return;
        }
    }
    int idx = etag_cache_clock % ETAG_CACHE_SIZE;
    etag_cache_clock++;
    etag_cache[idx].valid = 1;
    snprintf(etag_cache[idx].key, sizeof(etag_cache[idx].key), "%s", key);
    snprintf(etag_cache[idx].etag, sizeof(etag_cache[idx].etag), "\"%ld-%ld\"", mtime, size);
    strncpy(buf, etag_cache[idx].etag, buf_size);
}

/* ================================================================
 *  Gzip cache — cache compressed responses for small files
 * ================================================================ */
#define GZIP_CACHE_SIZE 512

typedef struct {
    char key[128];
    char *data;
    size_t data_len;
    int valid;
} GzipCacheEntry;

static GzipCacheEntry gzip_cache[GZIP_CACHE_SIZE];
static int gzip_cache_clock = 0;

static const char *gzip_cache_get(const char *key, size_t *out_len) {
    for (int i = 0; i < GZIP_CACHE_SIZE; i++) {
        if (gzip_cache[i].valid && strcmp(gzip_cache[i].key, key) == 0) {
            *out_len = gzip_cache[i].data_len;
            return gzip_cache[i].data;
        }
    }
    return NULL;
}

static void gzip_cache_set(const char *key, const char *data, size_t data_len) {
    int idx = gzip_cache_clock % GZIP_CACHE_SIZE;
    gzip_cache_clock++;
    if (gzip_cache[idx].valid) free(gzip_cache[idx].data);
    gzip_cache[idx].valid = 1;
    snprintf(gzip_cache[idx].key, sizeof(gzip_cache[idx].key), "%s", key);
    gzip_cache[idx].data = (char *)malloc(data_len);
    if (gzip_cache[idx].data) {
        memcpy(gzip_cache[idx].data, data, data_len);
        gzip_cache[idx].data_len = data_len;
    } else {
        gzip_cache[idx].valid = 0;
    }
}

/* ================================================================
 *  File content cache — cache file data for small files
 * ================================================================ */
#define FILE_CACHE_SIZE 128
#define FILE_CACHE_MAX_SIZE (256 * 1024)

typedef struct {
    char path[PATH_MAX];
    char *data;
    size_t data_len;
    time_t mtime;
    int valid;
} FileCacheEntry;

static FileCacheEntry file_cache[FILE_CACHE_SIZE];
static int file_cache_clock = 0;

static const char *file_cache_get(const char *path, time_t mtime, size_t *out_len) {
    for (int i = 0; i < FILE_CACHE_SIZE; i++) {
        if (file_cache[i].valid &&
            file_cache[i].mtime == mtime &&
            strcmp(file_cache[i].path, path) == 0) {
            *out_len = file_cache[i].data_len;
            return file_cache[i].data;
        }
    }
    return NULL;
}

static void file_cache_set(const char *path, const char *data, size_t data_len, time_t mtime) {
    int idx = file_cache_clock % FILE_CACHE_SIZE;
    file_cache_clock++;
    if (file_cache[idx].valid) free(file_cache[idx].data);
    file_cache[idx].valid = 1;
    strncpy(file_cache[idx].path, path, PATH_MAX - 1);
    file_cache[idx].data = (char *)malloc(data_len);
    if (file_cache[idx].data) {
        memcpy(file_cache[idx].data, data, data_len);
        file_cache[idx].data_len = data_len;
        file_cache[idx].mtime = mtime;
    } else {
        file_cache[idx].valid = 0;
    }
}

enum http_method { HTTP_GET = 0, HTTP_HEAD, HTTP_OPTIONS };

static volatile sig_atomic_t g_shutdown = 0;

static void signal_handler(int signum) {
    (void)signum;
    g_shutdown = 1;
}

_Static_assert(sizeof(ConnectionState) <= 16384, "ConnectionState too large");

static ssize_t write_all(int fd, const void *buf, size_t len) {
    const char *p = (const char *)buf;
    size_t remaining = len;
    while (remaining > 0) {
        ssize_t n = write(fd, p, remaining);
        if (n < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        p += n;
        remaining -= n;
    }
    return (ssize_t)len;
}

static const char *get_mime_type(const char *path) {
    const char *dot = strrchr(path, '.');
    if (!dot) return "application/octet-stream";
    const char *ext = dot + 1;
    if (strcmp(ext, "html") == 0 || strcmp(ext, "htm") == 0) return "text/html";
    if (strcmp(ext, "css") == 0) return "text/css";
    if (strcmp(ext, "js") == 0 || strcmp(ext, "mjs") == 0) return "application/javascript";
    if (strcmp(ext, "json") == 0) return "application/json";
    if (strcmp(ext, "txt") == 0) return "text/plain";
    if (strcmp(ext, "png") == 0) return "image/png";
    if (strcmp(ext, "jpg") == 0 || strcmp(ext, "jpeg") == 0) return "image/jpeg";
    if (strcmp(ext, "gif") == 0) return "image/gif";
    if (strcmp(ext, "svg") == 0) return "image/svg+xml";
    if (strcmp(ext, "ico") == 0) return "image/x-icon";
    if (strcmp(ext, "pdf") == 0) return "application/pdf";
    if (strcmp(ext, "bin") == 0) return "application/octet-stream";
    return "application/octet-stream";
}

static int compute_etag(long mtime, long size, char *buf, size_t buf_size) {
    return snprintf(buf, buf_size, "\"%ld-%ld\"", mtime, size);
}

static char *gzip_compress(const char *data, size_t data_len, size_t *out_len, int level) {
    z_stream strm;
    memset(&strm, 0, sizeof(strm));
    if (deflateInit2(&strm, level, Z_DEFLATED, 15 + 16, 8, Z_DEFAULT_STRATEGY) != Z_OK)
        return NULL;

    size_t max_out = data_len + (data_len >> 10) + 64;
    char *out = (char *)malloc(max_out);
    if (!out) { deflateEnd(&strm); return NULL; }

    strm.next_in = (Bytef *)data;
    strm.avail_in = (uInt)data_len;
    strm.next_out = (Bytef *)out;
    strm.avail_out = (uInt)max_out;

    if (deflate(&strm, Z_FINISH) != Z_STREAM_END) {
        free(out);
        deflateEnd(&strm);
        return NULL;
    }
    *out_len = strm.total_out;
    deflateEnd(&strm);
    return out;
}

static int url_decode(const char *src, char *dst, size_t dst_size) {
    size_t i = 0, j = 0;
    while (src[i] && j < dst_size - 1) {
        if (src[i] == '%' && src[i + 1] && src[i + 2]) {
            char hex[3] = { src[i + 1], src[i + 2], '\0' };
            dst[j++] = (char)strtol(hex, NULL, 16);
            i += 3;
        } else {
            dst[j++] = src[i++];
        }
    }
    dst[j] = '\0';
    if (j < dst_size - 1 && strlen(dst) != j) return -1;
    return (j < dst_size - 1) ? 0 : -1;
}

static int normalize_path(const char *raw, char *out, size_t out_size) {
    char decoded[MAX_PATH];
    if (url_decode(raw, decoded, sizeof(decoded)) < 0) return -1;

    /* Reject null bytes */
    if (strchr(decoded, '\0') != decoded + strlen(decoded)) return -1;

    /* Ensure starts with / */
    const char *p = decoded;
    if (*p != '/') {
        if (snprintf(out, out_size, "/%s", decoded) >= (int)out_size) return -1;
        p = out;
    } else {
        strncpy(out, decoded, out_size - 1);
        out[out_size - 1] = '\0';
        p = out;
    }

    /* Collapse double slashes */
    char *w = out;
    const char *r = p;
    while (*r) {
        if (*r == '/' && *(r + 1) == '/') {
            r++;
            continue;
        }
        *w++ = *r++;
    }
    *w = '\0';

    /* Resolve . and .. segments, reject if .. escapes root */
    char resolved[MAX_PATH];
    char *s = resolved, *end = resolved + sizeof(resolved) - 1;
    const char *src = out;
    *s = '\0';
    int segments = 0; /* Count of directory segments in resolved path */

    while (*src) {
        while (*src == '/') {
            if (s < end) *s++ = '/';
            src++;
        }
        if (*src == '\0') break;

        const char *seg_start = src;
        while (*src && *src != '/') src++;
        size_t seg_len = src - seg_start;

        if (seg_len == 1 && seg_start[0] == '.') {
            /* Skip . */
            if (s > resolved && *(s - 1) == '/') s--;
        } else if (seg_len == 2 && seg_start[0] == '.' && seg_start[1] == '.') {
            /* Go up .. - must have a segment to remove */
            if (segments <= 0) return -1; /* Escapes root */
            segments--;
            if (s > resolved && *(s - 1) == '/') s--;
            while (s > resolved && *(s - 1) != '/') s--;
        } else {
            if (s + seg_len >= end) return -1;
            memcpy(s, seg_start, seg_len);
            s += seg_len;
            segments++;
        }
    }
    if (s == resolved) *s++ = '/';
    *s = '\0';

    strncpy(out, resolved, out_size - 1);
    out[out_size - 1] = '\0';
    return 0;
}

static int resolve_path(const char *normalized, const char *root_dir, char *fs_path, size_t fs_path_size) {
    const char *rel = normalized + 1; /* Skip leading / */
    int ret = snprintf(fs_path, fs_path_size, "%s/%s", root_dir, rel);
    if (ret >= (int)fs_path_size) return -1;

    /* Security: resolved normalized path must stay under root_dir */
    char root_resolved[PATH_MAX];
    if (realpath(root_dir, root_resolved) == NULL) return -1;

    /* Normalize the combined path without requiring the file to exist */
    char norm_buf[PATH_MAX];
    strncpy(norm_buf, fs_path, sizeof(norm_buf) - 1);
    norm_buf[sizeof(norm_buf) - 1] = '\0';

    /* Walk the path manually to collapse . and .. without realpath on the file */
    char resolved[PATH_MAX];
    char *s = resolved, *end = resolved + sizeof(resolved) - 1;
    const char *src = norm_buf;
    *s = '\0';

    while (*src) {
        while (*src == '/') {
            if (s < end) *s++ = '/';
            src++;
        }
        if (*src == '\0') break;

        const char *seg_start = src;
        while (*src && *src != '/') src++;
        size_t seg_len = src - seg_start;

        if (seg_len == 1 && seg_start[0] == '.') {
            if (s > resolved && *(s - 1) == '/') s--;
        } else if (seg_len == 2 && seg_start[0] == '.' && seg_start[1] == '.') {
            if (s > resolved && *(s - 1) == '/') s--;
            while (s > resolved && *(s - 1) != '/') s--;
        } else {
            if (s + seg_len >= end) return -1;
            memcpy(s, seg_start, seg_len);
            s += seg_len;
        }
    }
    if (s == resolved) *s++ = '/';
    *s = '\0';

    /* Verify resolved path is under root */
    if (strncmp(resolved, root_resolved, strlen(root_resolved)) != 0) return -1;
    size_t root_len = strlen(root_resolved);
    if (resolved[root_len] != '/' && resolved[root_len] != '\0') return -1;

    strncpy(fs_path, resolved, fs_path_size - 1);
    fs_path[fs_path_size - 1] = '\0';
    return 0;
}

static void format_size(long size, char *buf, size_t buf_size) {
    if (size < 1024) {
        snprintf(buf, buf_size, "%ld B", size);
    } else if (size < 1024 * 1024) {
        snprintf(buf, buf_size, "%.0f KB", size / 1024.0);
    } else if (size < 1024L * 1024 * 1024) {
        snprintf(buf, buf_size, "%.1f MB", size / (1024.0 * 1024.0));
    } else {
        snprintf(buf, buf_size, "%.1f GB", size / (1024.0 * 1024.0 * 1024.0));
    }
}

static void format_date(time_t timestamp, char *buf, size_t buf_size) {
    struct tm *tm_info = localtime(&timestamp);
    strftime(buf, buf_size, "%Y-%m-%d %H:%M", tm_info);
}

typedef struct {
    char name[256];
    int is_dir;
    time_t mtime;
    off_t size;
} DirEntry;

static int cmp_dir_entry(const void *a, const void *b) {
    const DirEntry *ea = (const DirEntry *)a;
    const DirEntry *eb = (const DirEntry *)b;
    if (ea->is_dir != eb->is_dir) return eb->is_dir - ea->is_dir;
    return strcasecmp(ea->name, eb->name);
}

static void write_extra_headers(int client_fd, const char *origin, const char *url_path);
static void apply_common_headers(int client_fd);
static void apply_cors_headers(int client_fd, const char *origin);
static void apply_custom_headers(int client_fd, const char *url_path);
static int inject_live_reload_script(const char *content, size_t content_len,
                                     char **out_content, size_t *out_len);

static void send_error_page(int client_fd, int status, const char *root_dir) {
    const char *title, *message;
    switch (status) {
        case 403: title = "403 Forbidden"; message = "You don't have permission to access this resource."; break;
        case 404: title = "404 Not Found"; message = "The requested URL was not found on this server."; break;
        case 500: title = "500 Internal Server Error"; message = "The server encountered an internal error."; break;
        default: title = "Error"; message = "An error occurred.";
    }

    char body[1024];
    int body_len = snprintf(body, sizeof(body),
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>%s</title>"
        "<style>body{font-family:sans-serif;padding:40px;text-align:center}"
        "h1{font-weight:400;color:#333}</style></head>"
        "<body><h1>%d</h1><p>%s</p></body></html>",
        title, status, message);

    char header[512];
    int header_len = snprintf(header, sizeof(header),
        "HTTP/1.1 %d %s\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        "Content-Length: %d\r\n"
        "Server: ssserve\r\n"
        "Connection: close\r\n",
        status, title, body_len);

    write_all(client_fd, header, header_len);
    apply_common_headers(client_fd);
    write_all(client_fd, "\r\n", 2);
    write_all(client_fd, body, body_len);
}

static void send_error_page_for_file(int client_fd, int status, const char *root_dir) {
    char error_path[PATH_MAX];
    snprintf(error_path, sizeof(error_path), "%s/%d.html", root_dir, status);

    struct stat st;
    if (stat(error_path, &st) == 0) {
        int file_fd = open(error_path, O_RDONLY);
        if (file_fd >= 0) {
            char header_buf[512];
            int header_len = snprintf(header_buf, sizeof(header_buf),
                "HTTP/1.1 %d\r\n"
                "Content-Type: text/html; charset=utf-8\r\n"
                "Content-Length: %ld\r\n"
                "Server: ssserve\r\n"
                "Connection: close\r\n",
                status, (long)st.st_size);
            write_all(client_fd, header_buf, header_len);
            apply_common_headers(client_fd);
            write_all(client_fd, "\r\n", 2);

            if (st.st_size > SENDFILE_THRESHOLD) {
                off_t offset = 0;
                size_t remaining = st.st_size;
                while (remaining > 0) {
                    ssize_t sent = sendfile(client_fd, file_fd, &offset, remaining);
                    if (sent < 0) {
                        if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) continue;
                        break;
                    }
                    remaining -= sent;
                }
            } else {
                char file_buf[BUFFER_SIZE];
                ssize_t bytes_read;
                while ((bytes_read = read(file_fd, file_buf, sizeof(file_buf))) > 0) {
                    if (write_all(client_fd, file_buf, bytes_read) < 0) break;
                }
            }
            close(file_fd);
            return;
        }
    }

    send_error_page(client_fd, status, root_dir);
}

static void render_listing(int client_fd, const char *fs_path, const char *url_path, const char *origin) {
    DIR *dir = opendir(fs_path);
    if (!dir) {
        send_error_page_for_file(client_fd, 403, server_config.root_dir);
        return;
    }

    DirEntry entries[4096];
    int count = 0;
    struct dirent *ent;
    while ((ent = readdir(dir)) != NULL && count < 4096) {
        if (ent->d_name[0] == '.') {
            if (strcmp(ent->d_name, ".") == 0 || strcmp(ent->d_name, "..") == 0) continue;
            if (strcmp(ent->d_name, ".DS_Store") == 0) continue;
            if (strcmp(ent->d_name, ".git") == 0) continue;
        }
        strncpy(entries[count].name, ent->d_name, sizeof(entries[count].name) - 1);
        entries[count].name[sizeof(entries[count].name) - 1] = '\0';

        char entry_path[PATH_MAX];
        snprintf(entry_path, sizeof(entry_path), "%s/%s", fs_path, ent->d_name);
        struct stat st;
        if (stat(entry_path, &st) == 0) {
            entries[count].is_dir = S_ISDIR(st.st_mode);
            entries[count].mtime = st.st_mtime;
            entries[count].size = st.st_size;
        } else {
            entries[count].is_dir = 0;
            entries[count].mtime = 0;
            entries[count].size = 0;
        }
        count++;
    }
    closedir(dir);

    qsort(entries, count, sizeof(DirEntry), cmp_dir_entry);

    /* Build HTML */
    char *body = NULL;
    size_t body_len = 0;
    size_t body_cap = 8192;
    body = (char *)malloc(body_cap);
    if (!body) {
        send_error_page_for_file(client_fd, 500, server_config.root_dir);
        return;
    }

    #define APPEND(s) do { \
        size_t slen = strlen(s); \
        if (body_len + slen >= body_cap) { \
            body_cap = (body_cap + slen) * 2; \
            body = realloc(body, body_cap); \
            if (!body) { send_error_page_for_file(client_fd, 500, server_config.root_dir); return; } \
        } \
        memcpy(body + body_len, s, slen); \
        body_len += slen; \
        body[body_len] = '\0'; \
    } while(0)

    char tmp[1024];
    snprintf(tmp, sizeof(tmp),
        "<!DOCTYPE html>\n<html>\n<head>\n"
        "<meta charset=\"UTF-8\">\n"
        "<title>Index of %s</title>\n"
        "<style>\n"
        "body { font-family: sans-serif; margin: 2em; }\n"
        "table { border-collapse: collapse; width: 100%; }\n"
        "th, td { text-align: left; padding: 0.5em 1em; }\n"
        "th { border-bottom: 2px solid #333; }\n"
        "tr:hover { background: #f5f5f5; }\n"
        "a { text-decoration: none; color: #0366d6; }\n"
        "a:hover { text-decoration: underline; }\n"
        ".icon { width: 2em; display: inline-block; text-align: center; }\n"
        "</style>\n"
        "</head>\n<body>\n"
        "<h1>Index of %s</h1>\n"
        "<table>\n"
        "<tr><th>Name</th><th>Size</th><th>Last Modified</th></tr>\n",
        url_path, url_path);
    APPEND(tmp);

    for (int i = 0; i < count; i++) {
        char size_str[32], date_str[32];
        if (entries[i].is_dir) {
            format_date(entries[i].mtime, date_str, sizeof(date_str));
            snprintf(tmp, sizeof(tmp),
                "<tr><td><a href=\"%s/\">%s/</a></td><td>-</td><td>%s</td></tr>\n",
                entries[i].name, entries[i].name, date_str);
        } else {
            format_size(entries[i].size, size_str, sizeof(size_str));
            format_date(entries[i].mtime, date_str, sizeof(date_str));
            snprintf(tmp, sizeof(tmp),
                "<tr><td><a href=\"%s\">%s</a></td><td>%s</td><td>%s</td></tr>\n",
                entries[i].name, entries[i].name, size_str, date_str);
        }
        APPEND(tmp);
    }

    APPEND("</table>\n</body>\n</html>\n");
    #undef APPEND

    /* Inject live-reload script into directory listing */
    if (server_config.live_reload) {
        char *lr_content = NULL;
        size_t lr_len = 0;
        if (inject_live_reload_script(body, body_len, &lr_content, &lr_len)) {
            free(body);
            body = lr_content;
            body_len = lr_len;
        }
    }

    char header_buf[512];
    int header_len = snprintf(header_buf, sizeof(header_buf),
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        "Content-Length: %zu\r\n"
        "Server: ssserve\r\n",
        body_len);
    write_all(client_fd, header_buf, header_len);
    write_extra_headers(client_fd, origin, url_path);
    write_all(client_fd, "\r\n", 2);
    write_all(client_fd, body, body_len);
    free(body);
}

static int parse_http_request(ConnectionState *conn) {
    char *method_end = strchr(conn->buffer, ' ');
    if (!method_end) return -1;

    *method_end = '\0';
    if (strcmp(conn->buffer, "GET") == 0) conn->method = HTTP_GET;
    else if (strcmp(conn->buffer, "HEAD") == 0) conn->method = HTTP_HEAD;
    else if (strcmp(conn->buffer, "OPTIONS") == 0) conn->method = HTTP_OPTIONS;
    else return -1;

    char *path_start = method_end + 1;
    char *path_end = strchr(path_start, ' ');
    if (!path_end) return -1;

    *path_end = '\0';
    strncpy(conn->path, path_start, MAX_PATH - 1);
    conn->path[MAX_PATH - 1] = '\0';

    char *headers_start = strstr(path_end + 1, "\r\n");
    if (!headers_start) return -1;

    char *header = headers_start + 2;
    while (header && *header && strncmp(header, "\r\n", 2) != 0) {
        char *next_header = strstr(header, "\r\n");
        if (!next_header) break;

        if (strncmp(header, "Host:", 5) == 0) {
            conn->host = header + 5;
            while (*conn->host == ' ') conn->host++;
            char *end = strstr(conn->host, "\r\n");
            if (end) *end = '\0';
        } else if (strncmp(header, "Accept-Encoding:", 16) == 0) {
            conn->accept_encoding = header + 16;
            while (*conn->accept_encoding == ' ') conn->accept_encoding++;
            char *end = strstr(conn->accept_encoding, "\r\n");
            if (end) *end = '\0';
        } else if (strncmp(header, "If-None-Match:", 14) == 0) {
            conn->if_none_match = header + 14;
            while (*conn->if_none_match == ' ') conn->if_none_match++;
            char *end = strstr(conn->if_none_match, "\r\n");
            if (end) *end = '\0';
        } else if (strncmp(header, "If-Modified-Since:", 18) == 0) {
            conn->if_modified_since = header + 18;
            while (*conn->if_modified_since == ' ') conn->if_modified_since++;
            char *end = strstr(conn->if_modified_since, "\r\n");
            if (end) *end = '\0';
        } else if (strncmp(header, "Range:", 6) == 0) {
            conn->range_header = header + 6;
            while (*conn->range_header == ' ') conn->range_header++;
            char *end = strstr(conn->range_header, "\r\n");
            if (end) *end = '\0';
        } else if (strncmp(header, "Origin:", 7) == 0) {
            conn->origin = header + 7;
            while (*conn->origin == ' ') conn->origin++;
            char *end = strstr(conn->origin, "\r\n");
            if (end) *end = '\0';
        }
        header = next_header + 2;
    }

    return 0;
}

static void send_redirect(int client_fd, const char *location, int status) {
    char header[1024];
    int len = snprintf(header, sizeof(header),
        "HTTP/1.1 %d Moved Permanently\r\n"
        "Location: %s\r\n"
        "Content-Length: 0\r\n"
        "Server: ssserve\r\n"
        "Connection: close\r\n",
        status, location);
    write_all(client_fd, header, len);
    write_extra_headers(client_fd, NULL, NULL);
    write_all(client_fd, "\r\n", 2);
}

static void apply_cors_headers(int client_fd, const char *origin) {
    if (!server_config.cors) return;
    char headers[512];
    int len = snprintf(headers, sizeof(headers),
        "Access-Control-Allow-Origin: *\r\n"
        "Access-Control-Allow-Headers: *\r\n"
        "Access-Control-Allow-Methods: GET, HEAD, OPTIONS\r\n"
        "Access-Control-Allow-Credentials: true\r\n");
    write_all(client_fd, headers, len);
}

static void apply_common_headers(int client_fd) {
    if (!server_config.caching) {
        write_all(client_fd, "Cache-Control: no-store\r\n", 25);
    }
}

static void apply_custom_headers(int client_fd, const char *url_path) {
    if (!url_path) return;
    for (int i = 0; i < server_config.num_header_rules; i++) {
        HeaderRule *rule = &server_config.header_rules[i];
        if (fnmatch(rule->pattern, url_path, 0) == 0) {
            for (int j = 0; j < rule->count; j++) {
                write_all(client_fd, rule->lines[j], strlen(rule->lines[j]));
                write_all(client_fd, "\r\n", 2);
            }
        }
    }
}

static void write_extra_headers(int client_fd, const char *origin, const char *url_path) {
    apply_custom_headers(client_fd, url_path);
    apply_cors_headers(client_fd, origin);
    apply_common_headers(client_fd);
}

static int check_callbacks(ConnectionState *conn, char *new_path, size_t new_path_size, int *status_code) {
    if (!server_config.config_callback || server_config.config_callback == Py_None) return 0;

    PyGILState_STATE gstate = PyGILState_Ensure();
    PyObject *result = PyObject_CallFunction(server_config.config_callback, "s", conn->path);
    if (result == NULL) {
        PyErr_Print();
        PyGILState_Release(gstate);
        return 0;
    }
    if (result == Py_None) {
        Py_DECREF(result);
        PyGILState_Release(gstate);
        return 0;
    }

    // Result is (new_path, status_code) tuple or None
    if (PyTuple_Check(result) && PyTuple_Size(result) == 2) {
        PyObject *path_obj = PyTuple_GetItem(result, 0);
        PyObject *status_obj = PyTuple_GetItem(result, 1);
        const char *path = PyUnicode_AsUTF8(path_obj);
        int status = (int)PyLong_AsLong(status_obj);
        if (path && status > 0) {
            snprintf(new_path, new_path_size, "%s", path);
            *status_code = status;
            Py_DECREF(result);
            PyGILState_Release(gstate);
            return 1;
        }
    }
    Py_DECREF(result);
    PyGILState_Release(gstate);
    return 0;
}

static void handle_live_reload_check(ConnectionState *conn, const char *normalized_path) {
    PyGILState_STATE gstate = PyGILState_Ensure();

    /* Parse client version from query string ?v=N */
    int client_version = 0;
    const char *query = strchr(normalized_path, '?');
    if (query) {
        query++;
        const char *v_param = strstr(query, "v=");
        if (v_param) {
            client_version = atoi(v_param + 2);
        }
    }

    /* Get current version from LiveReload object */
    PyObject *version_obj = PyObject_GetAttrString(server_config.live_reload, "version");
    if (!version_obj) {
        PyErr_Print();
        PyGILState_Release(gstate);
        char response[] = "HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\nServer: ssserve\r\n";
        write_all(conn->fd, response, strlen(response));
        return;
    }
    int current_version = (int)PyLong_AsLong(version_obj);
    Py_DECREF(version_obj);

    int should_reload = current_version > client_version;

    char body[128];
    int body_len = snprintf(body, sizeof(body),
        "{\"reload\":%s,\"version\":%d}",
        should_reload ? "true" : "false",
        current_version);

    char header_buf[256];
    int header_len = snprintf(header_buf, sizeof(header_buf),
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: %d\r\n"
        "Cache-Control: no-store\r\n"
        "Server: ssserve\r\n",
        body_len);

    write_all(conn->fd, header_buf, header_len);
    write_extra_headers(conn->fd, conn->origin, conn->path);
    write_all(conn->fd, "\r\n", 2);
    write_all(conn->fd, body, body_len);

    PyGILState_Release(gstate);
}

static int inject_live_reload_script(const char *content, size_t content_len,
                                     char **out_content, size_t *out_len) {
    PyGILState_STATE gstate = PyGILState_Ensure();

    PyObject *py_content = PyUnicode_FromStringAndSize(content, content_len);
    if (!py_content) {
        PyErr_Print();
        PyGILState_Release(gstate);
        return 0;
    }

    PyObject *version_obj = PyObject_GetAttrString(server_config.live_reload, "version");
    if (!version_obj) {
        PyErr_Print();
        Py_DECREF(py_content);
        PyGILState_Release(gstate);
        return 0;
    }
    int version = (int)PyLong_AsLong(version_obj);
    Py_DECREF(version_obj);

    PyObject *result = PyObject_CallMethod(server_config.live_reload, "inject_script", "Oi",
                                           py_content, version);
    Py_DECREF(py_content);

    if (!result) {
        PyErr_Print();
        PyGILState_Release(gstate);
        return 0;
    }

    const char *result_str = PyUnicode_AsUTF8AndSize(result, (Py_ssize_t *)out_len);
    if (!result_str) {
        PyErr_Print();
        Py_DECREF(result);
        PyGILState_Release(gstate);
        return 0;
    }

    *out_content = strdup(result_str);
    *out_len = strlen(*out_content);
    Py_DECREF(result);
    PyGILState_Release(gstate);
    return 1;
}

static int parse_range(const char *range_header, int file_size, int *start, int *end) {
    if (!range_header || strncmp(range_header, "bytes=", 6) != 0) return -1;
    const char *p = range_header + 6;
    if (*p == '-') {
        int suffix = atoi(p + 1);
        if (suffix <= 0 || suffix > file_size) return -1;
        *start = file_size - suffix;
        *end = file_size - 1;
    } else {
        *start = atoi(p);
        char *dash = strchr(p, '-');
        if (!dash) return -1;
        if (*(dash + 1) == '\0') {
            *end = file_size - 1;
        } else {
            *end = atoi(dash + 1);
        }
        if (*start < 0 || *end >= file_size || *start > *end) return -1;
    }
    return 0;
}

static void handle_request(ConnectionState *conn) {
    if (conn->method == HTTP_OPTIONS) {
        char response[] =
            "HTTP/1.1 204 No Content\r\n"
            "Allow: GET, HEAD, OPTIONS\r\n"
            "Content-Length: 0\r\n"
            "Server: ssserve\r\n";
        write_all(conn->fd, response, strlen(response));
        write_extra_headers(conn->fd, conn->origin, conn->path);
        write_all(conn->fd, "\r\n", 2);
        return;
    }

    /* Normalize path: URL decode, collapse slashes, resolve . and .., security checks */
    char normalized[MAX_PATH];
    if (normalize_path(conn->path, normalized, sizeof(normalized)) < 0) {
        char response[] = "HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n";
        write_all(conn->fd, response, strlen(response));
        write_extra_headers(conn->fd, conn->origin, NULL);
        write_all(conn->fd, "\r\n", 2);
        return;
    }

    /* Handle live-reload check endpoint */
    if (server_config.live_reload && strncmp(normalized, "/__ssserve/", 11) == 0) {
        handle_live_reload_check(conn, normalized);
        return;
    }

    /* Check for clean URLs: /foo.html → redirect to /foo */
    if (server_config.clean_urls && conn->method == HTTP_GET) {
        size_t path_len = strlen(normalized);
        if (path_len > 5 && strcmp(normalized + path_len - 5, ".html") == 0) {
            char new_path[MAX_PATH];
            if (strcmp(normalized, "/index.html") == 0) {
                snprintf(new_path, sizeof(new_path), "/");
            } else {
                snprintf(new_path, sizeof(new_path), "%.*s", (int)(path_len - 5), normalized);
            }
            /* Only redirect if the .html file exists (verified by resolving with .html) */
            char html_full_path[PATH_MAX];
            if (resolve_path(normalized, server_config.root_dir, html_full_path, sizeof(html_full_path)) == 0) {
                /* Skip symlinks if symlinks disabled */
                if (!server_config.symlinks) {
                    struct stat html_lst;
                    if (lstat(html_full_path, &html_lst) == 0 && S_ISLNK(html_lst.st_mode)) {
                        /* Don't redirect, fall through to normal handling */
                    } else {
                        struct stat html_st;
                        if (stat(html_full_path, &html_st) == 0) {
                            send_redirect(conn->fd, new_path, 301);
                            return;
                        }
                    }
                } else {
                    struct stat html_st;
                    if (stat(html_full_path, &html_st) == 0) {
                        send_redirect(conn->fd, new_path, 301);
                        return;
                    }
                }
            }
        }
    }

    /* Check Python callbacks for redirects/rewrites */
    char callback_path[MAX_PATH];
    int callback_status;
    if (check_callbacks(conn, callback_path, sizeof(callback_path), &callback_status)) {
        if (callback_status != 200) {
            /* Redirect: send redirect response and return */
            send_redirect(conn->fd, callback_path, callback_status);
            return;
        }
        /* Rewrite: update path and continue processing */
        strncpy(conn->path, callback_path, MAX_PATH - 1);
        conn->path[MAX_PATH - 1] = '\0';
        /* Re-normalize the rewritten path */
        if (normalize_path(conn->path, normalized, sizeof(normalized)) < 0) {
            char response[] = "HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n";
            write_all(conn->fd, response, strlen(response));
            write_extra_headers(conn->fd, conn->origin, NULL);
            write_all(conn->fd, "\r\n", 2);
            return;
        }
    }

    char full_path[PATH_MAX];
    if (resolve_path(normalized, server_config.root_dir, full_path, sizeof(full_path)) < 0) {
        char response[] = "HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n";
        write_all(conn->fd, response, strlen(response));
        write_extra_headers(conn->fd, conn->origin, NULL);
        write_all(conn->fd, "\r\n", 2);
        return;
    }

    struct stat st;
    struct stat lst;
    if (lstat(full_path, &lst) < 0) {
        /* File not found - try clean URLs and SPA fallback */
        /* Try appending .html for clean URLs (e.g., /about → /about.html) */
        if (server_config.clean_urls) {
            size_t norm_len = strlen(normalized);
            if (norm_len < MAX_PATH - 5) {
                char html_path[PATH_MAX];
                snprintf(html_path, sizeof(html_path), "%s.html", normalized);
                char html_full_path[PATH_MAX];
                if (resolve_path(html_path, server_config.root_dir, html_full_path, sizeof(html_full_path)) == 0) {
                    if (stat(html_full_path, &st) == 0 && S_ISREG(st.st_mode)) {
                        snprintf(full_path, sizeof(full_path), "%s", html_full_path);
                        strncpy(conn->path, html_path, MAX_PATH - 1);
                        conn->path[MAX_PATH - 1] = '\0';
                        goto serve_file;
                    }
                }
            }
        }
        /* SPA fallback: serve index.html for 404 */
        if (server_config.single) {
            char index_path[PATH_MAX];
            snprintf(index_path, sizeof(index_path), "%s/index.html", server_config.root_dir);
            if (stat(index_path, &st) == 0 && S_ISREG(st.st_mode)) {
                snprintf(full_path, sizeof(full_path), "%s", index_path);
                snprintf(conn->path, MAX_PATH, "/index.html");
                goto serve_file;
            }
        }
        send_error_page_for_file(conn->fd, 404, server_config.root_dir);
        return;
    }

    /* Check symlinks: if symlinks disabled and path is a symlink, reject */
    if (!server_config.symlinks && S_ISLNK(lst.st_mode)) {
        send_error_page_for_file(conn->fd, 404, server_config.root_dir);
        return;
    }

    /* Use stat() to get info about the target (follows symlinks) */
    if (stat(full_path, &st) < 0) {
        send_error_page_for_file(conn->fd, 404, server_config.root_dir);
        return;
    }

    /* Trailing slash handling */
    if (server_config.trailing_slash >= 0) {
        size_t path_len = strlen(normalized);
        int has_slash = (path_len > 0 && normalized[path_len - 1] == '/');
        if (S_ISDIR(st.st_mode)) {
            /* Directory: redirect based on trailing_slash config */
            if (!has_slash && server_config.trailing_slash) {
                /* trailing_slash=True, no slash → add slash */
                char redirect_url[MAX_PATH + 32];
                snprintf(redirect_url, sizeof(redirect_url), "%s/", normalized);
                send_redirect(conn->fd, redirect_url, 301);
                return;
            }
            if (has_slash && !server_config.trailing_slash) {
                /* trailing_slash=False, has slash → remove slash */
                char redirect_url[MAX_PATH];
                if (path_len > 1) {
                    snprintf(redirect_url, sizeof(redirect_url), "%.*s", (int)(path_len - 1), normalized);
                } else {
                    snprintf(redirect_url, sizeof(redirect_url), "/");
                }
                send_redirect(conn->fd, redirect_url, 301);
                return;
            }
        } else if (S_ISREG(st.st_mode)) {
            /* File: redirect if trailing slash present and trailing_slash=False */
            if (has_slash && !server_config.trailing_slash) {
                char redirect_url[MAX_PATH];
                if (path_len > 1) {
                    snprintf(redirect_url, sizeof(redirect_url), "%.*s", (int)(path_len - 1), normalized);
                } else {
                    snprintf(redirect_url, sizeof(redirect_url), "/");
                }
                send_redirect(conn->fd, redirect_url, 301);
                return;
            }
        }
    }

    if (S_ISDIR(st.st_mode)) {
        /* If URL doesn't end with /, redirect */
        size_t path_len = strlen(normalized);
        if (path_len > 0 && normalized[path_len - 1] != '/') {
            char redirect_url[MAX_PATH + 32];
            snprintf(redirect_url, sizeof(redirect_url), "%s/", normalized);
            char response[MAX_PATH + 128];
            int resp_len = snprintf(response, sizeof(response),
                "HTTP/1.1 301 Moved Permanently\r\n"
                "Location: %s\r\n"
                "Content-Length: 0\r\n"
                "Server: ssserve\r\n",
                redirect_url);
            write_all(conn->fd, response, resp_len);
            write_extra_headers(conn->fd, conn->origin, normalized);
            write_all(conn->fd, "\r\n", 2);
            return;
        }

        /* Check for index.html */
        char index_path[PATH_MAX];
        snprintf(index_path, sizeof(index_path), "%s/index.html", full_path);
        if (stat(index_path, &st) == 0) {
            /* Build URL path for header matching: normalized.rstrip("/") + "/index.html" */
            size_t norm_len = strlen(normalized);
            if (norm_len > 0 && normalized[norm_len - 1] == '/') {
                snprintf(conn->path, MAX_PATH, "%sindex.html", normalized);
            } else {
                snprintf(conn->path, MAX_PATH, "%s/index.html", normalized);
            }
            snprintf(full_path, sizeof(full_path), "%s", index_path);
            goto serve_file;
        }

        /* Check for index.htm */
        snprintf(index_path, sizeof(index_path), "%s/index.htm", full_path);
        if (stat(index_path, &st) == 0) {
            snprintf(full_path, sizeof(full_path), "%s", index_path);
            goto serve_file;
        }

        /* Directory listing */
        render_listing(conn->fd, full_path, normalized, conn->origin);
        return;
    }

serve_file:

    const char *mime_type = get_mime_type(full_path);
    char header_buf[512];
    int header_len;

    int etag_enabled = server_config.etag;
    char etag_buf[64] = {0};
    char date_buf[64] = {0};
    if (etag_enabled) {
        etag_cache_get((long)st.st_mtime, (long)st.st_size, etag_buf, sizeof(etag_buf));
    } else {
        struct tm *gmt = gmtime(&st.st_mtime);
        strftime(date_buf, sizeof(date_buf), "%a, %d %b %Y %H:%M:%S GMT", gmt);
    }

    /* 304 Not Modified: check ETag first, then If-Modified-Since as fallback */
    int etag_match = 0;
    if (etag_enabled && conn->if_none_match) {
        if (strcmp(conn->if_none_match, etag_buf) == 0) {
            etag_match = 1;
        }
    }

    if (etag_match) {
        header_len = snprintf(header_buf, sizeof(header_buf),
            "HTTP/1.1 304 Not Modified\r\n"
            "ETag: %s\r\n"
            "Server: ssserve\r\n",
            etag_buf);
        write_all(conn->fd, header_buf, header_len);
        write_extra_headers(conn->fd, conn->origin, conn->path);
        write_all(conn->fd, "\r\n", 2);
        return;
    }

    if (conn->if_modified_since) {
        struct tm ims_tm;
        memset(&ims_tm, 0, sizeof(ims_tm));
        if (strptime(conn->if_modified_since, "%a, %d %b %Y %H:%M:%S", &ims_tm) != NULL) {
            time_t ims_time = timegm(&ims_tm);
            if ((long)st.st_mtime <= (long)ims_time) {
                header_len = snprintf(header_buf, sizeof(header_buf),
                    "HTTP/1.1 304 Not Modified\r\n"
                    "%s%s\r\n"
                    "Server: ssserve\r\n",
                    etag_enabled ? "ETag: " : "",
                    etag_enabled ? etag_buf : "");
                write_all(conn->fd, header_buf, header_len);
                write_extra_headers(conn->fd, conn->origin, conn->path);
                write_all(conn->fd, "\r\n", 2);
                return;
            }
        }
    }

    if (conn->method == HTTP_HEAD) {
        header_len = snprintf(header_buf, sizeof(header_buf),
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: %s\r\n"
            "Content-Length: %ld\r\n"
            "Server: ssserve\r\n"
            "%s%s\r\n"
            "%s",
            mime_type, (long)st.st_size,
            etag_enabled ? "ETag: " : "Last-Modified: ",
            etag_enabled ? etag_buf : date_buf,
            etag_enabled ? "" : "Accept-Ranges: bytes\r\n");
        write_all(conn->fd, header_buf, header_len);
        write_extra_headers(conn->fd, conn->origin, conn->path);
        write_all(conn->fd, "\r\n", 2);
        return;
    }

    /* Check for Range request */
    int range_start = 0, range_end = 0;
    int range_valid = 0;
    if (conn->range_header && parse_range(conn->range_header, (int)st.st_size, &range_start, &range_end) == 0) {
        range_valid = 1;
    }

    /* Check if gzip is acceptable (skip gzip for range requests and no_compression) */
    int use_gzip = 0;
    if (!range_valid && !server_config.no_compression && conn->accept_encoding && strstr(conn->accept_encoding, "gzip")) {
        use_gzip = 1;
    }

    /* Check if live-reload script injection is needed */
    int use_inject = 0;
    if (server_config.live_reload && !range_valid && conn->method != HTTP_HEAD && strstr(mime_type, "text/html")) {
        use_inject = 1;
    }

    char *injected_data = NULL;
    size_t injected_len = 0;
    int injected = 0;

    int file_fd = open(full_path, O_RDONLY);
    if (file_fd < 0) {
        send_error_page_for_file(conn->fd, 500, server_config.root_dir);
        return;
    }

    /* For live-reload injection: read file, inject script */
    if (use_inject) {
        char *file_data = (char *)malloc(st.st_size);
        if (file_data) {
            ssize_t total_read = 0;
            while (total_read < st.st_size) {
                ssize_t n = read(file_fd, file_data + total_read, st.st_size - total_read);
                if (n <= 0) break;
                total_read += n;
            }
            close(file_fd);

            if (inject_live_reload_script(file_data, total_read, &injected_data, &injected_len)) {
                injected = 1;
                free(file_data);
            } else {
                /* Injection failed, serve original file */
                free(file_data);
                file_fd = open(full_path, O_RDONLY);
                if (file_fd < 0) {
                    send_error_page_for_file(conn->fd, 500, server_config.root_dir);
                    return;
                }
            }
        }
    }

    /* For gzip: read file into memory, compress, and send */
    if (use_gzip && st.st_size < 256 * 1024 && st.st_size > 0) {
        char *file_data = NULL;
        size_t file_data_len = 0;
        int file_data_from_inject = 0;

        if (injected) {
            /* Use already-injected content */
            file_data = injected_data;
            file_data_len = injected_len;
            file_data_from_inject = 1;
        } else {
            file_data = (char *)malloc(st.st_size);
            if (file_data) {
                ssize_t total_read = 0;
                while (total_read < st.st_size) {
                    ssize_t n = read(file_fd, file_data + total_read, st.st_size - total_read);
                    if (n <= 0) break;
                    total_read += n;
                }
                file_data_len = total_read;
            }
        }

        if (file_data) {
            close(file_fd);

            /* Try gzip cache first */
            char gzip_key[256];
            snprintf(gzip_key, sizeof(gzip_key), "%s%ld:%ld",
                     injected ? "lr:" : "", (long)st.st_mtime, (long)st.st_size);
            size_t cached_gzip_len = 0;
            const char *cached_gzip = gzip_cache_get(gzip_key, &cached_gzip_len);

            char *compressed = NULL;
            size_t compressed_len = 0;

            if (cached_gzip) {
                compressed = (char *)malloc(cached_gzip_len);
                if (compressed) {
                    memcpy(compressed, cached_gzip, cached_gzip_len);
                    compressed_len = cached_gzip_len;
                }
            }

            if (!compressed) {
                compressed = gzip_compress(file_data, file_data_len, &compressed_len, 6);
                if (compressed) gzip_cache_set(gzip_key, compressed, compressed_len);
            }
            if (!file_data_from_inject) free(file_data);

            if (compressed) {
                header_len = snprintf(header_buf, sizeof(header_buf),
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: %s\r\n"
                    "Content-Length: %zu\r\n"
                    "Content-Encoding: gzip\r\n"
                    "Server: ssserve\r\n"
                    "%s%s\r\n",
                    mime_type, compressed_len,
                    etag_enabled ? "ETag: " : "Last-Modified: ",
                    etag_enabled ? etag_buf : date_buf);
                write_all(conn->fd, header_buf, header_len);
                write_extra_headers(conn->fd, conn->origin, conn->path);
                write_all(conn->fd, "\r\n", 2);
                write_all(conn->fd, compressed, compressed_len);
                free(compressed);
                if (injected) free(injected_data);
                return;
            }
            /* gzip failed, fall through to normal send */
            file_fd = open(full_path, O_RDONLY);
            if (file_fd < 0) {
                if (injected) free(injected_data);
                send_error_page_for_file(conn->fd, 500, server_config.root_dir);
                return;
            }
            if (injected) { free(injected_data); injected = 0; }
        }
    }

    if (range_valid) {
        int range_size = range_end - range_start + 1;
        header_len = snprintf(header_buf, sizeof(header_buf),
            "HTTP/1.1 206 Partial Content\r\n"
            "Content-Type: %s\r\n"
            "Content-Length: %d\r\n"
            "Content-Range: bytes %d-%d/%ld\r\n"
            "Server: ssserve\r\n"
            "%s%s\r\n",
            mime_type, range_size, range_start, range_end, (long)st.st_size,
            etag_enabled ? "ETag: " : "Last-Modified: ",
            etag_enabled ? etag_buf : date_buf);
        write_all(conn->fd, header_buf, header_len);
        write_extra_headers(conn->fd, conn->origin, conn->path);
        write_all(conn->fd, "\r\n", 2);

        lseek(file_fd, range_start, SEEK_SET);
        if (range_size > SENDFILE_THRESHOLD) {
            off_t offset = range_start;
            size_t remaining = range_size;
            while (remaining > 0) {
                ssize_t sent = sendfile(conn->fd, file_fd, &offset, remaining);
                if (sent < 0) {
                    if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) continue;
                    break;
                }
                remaining -= sent;
            }
        } else {
            char file_buf[BUFFER_SIZE];
            ssize_t bytes_read;
            int to_read = range_size;
            while (to_read > 0 && (bytes_read = read(file_fd, file_buf, sizeof(file_buf))) > 0) {
                if (bytes_read > to_read) bytes_read = to_read;
                if (write_all(conn->fd, file_buf, bytes_read) < 0) break;
                to_read -= bytes_read;
            }
        }
    } else {
        if (injected) {
            header_len = snprintf(header_buf, sizeof(header_buf),
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: %s\r\n"
                "Content-Length: %zu\r\n"
                "Server: ssserve\r\n"
                "%s%s\r\n"
                "%s",
                mime_type, injected_len,
                etag_enabled ? "ETag: " : "Last-Modified: ",
                etag_enabled ? etag_buf : date_buf,
                etag_enabled ? "" : "Accept-Ranges: bytes\r\n");
            write_all(conn->fd, header_buf, header_len);
            write_extra_headers(conn->fd, conn->origin, conn->path);
            write_all(conn->fd, "\r\n", 2);
            write_all(conn->fd, injected_data, injected_len);
            free(injected_data);
        } else {
            /* Try file cache for small files */
            size_t cached_file_len = 0;
            const char *cached_file = NULL;
            if (st.st_size > 0 && st.st_size <= FILE_CACHE_MAX_SIZE) {
                cached_file = file_cache_get(full_path, st.st_mtime, &cached_file_len);
            }

            header_len = snprintf(header_buf, sizeof(header_buf),
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: %s\r\n"
                "Content-Length: %ld\r\n"
                "Server: ssserve\r\n"
                "%s%s\r\n"
                "%s",
                mime_type, (long)st.st_size,
                etag_enabled ? "ETag: " : "Last-Modified: ",
                etag_enabled ? etag_buf : date_buf,
                etag_enabled ? "" : "Accept-Ranges: bytes\r\n");
            write_all(conn->fd, header_buf, header_len);
            write_extra_headers(conn->fd, conn->origin, conn->path);
            write_all(conn->fd, "\r\n", 2);

            if (cached_file) {
                /* Serve from file cache */
                write_all(conn->fd, cached_file, cached_file_len);
            } else if (st.st_size > SENDFILE_THRESHOLD) {
                off_t offset = 0;
                size_t remaining = st.st_size;
                while (remaining > 0) {
                    ssize_t sent = sendfile(conn->fd, file_fd, &offset, remaining);
                    if (sent < 0) {
                        if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) continue;
                        break;
                    }
                    remaining -= sent;
                }
            } else {
                /* Small file, not cached — read into cache then serve */
                char *file_data = (char *)malloc(st.st_size);
                if (file_data) {
                    ssize_t total_read = 0;
                    while (total_read < st.st_size) {
                        ssize_t n = read(file_fd, file_data + total_read, st.st_size - total_read);
                        if (n <= 0) break;
                        total_read += n;
                    }
                    file_cache_set(full_path, file_data, total_read, st.st_mtime);
                    write_all(conn->fd, file_data, total_read);
                    free(file_data);
                } else {
                    char file_buf[BUFFER_SIZE];
                    ssize_t bytes_read;
                    while ((bytes_read = read(file_fd, file_buf, sizeof(file_buf))) > 0) {
                        if (write_all(conn->fd, file_buf, bytes_read) < 0) break;
                    }
                }
            }
        }
    }

    close(file_fd);
}

static void *worker_thread(void *arg) {
    (void)arg;
    struct epoll_event events[MAX_EVENTS];

    while (!g_shutdown) {
        int nfds = epoll_wait(server_config.epoll_fd, events, MAX_EVENTS, 1000);
        if (nfds < 0) {
            if (errno == EINTR) continue;
            break;
        }

        for (int i = 0; i < nfds; i++) {
            if (events[i].data.fd == server_config.listen_fd) {
                /* Drain all pending connections (edge-triggered requires EAGAIN loop) */
                while (1) {
                    int client_fd = accept(server_config.listen_fd, NULL, NULL);
                    if (client_fd < 0) {
                        if (errno == EAGAIN || errno == EWOULDBLOCK) break;
                        break;
                    }

                    int flags = fcntl(client_fd, F_GETFL, 0);
                    if (flags < 0 || fcntl(client_fd, F_SETFL, flags | O_NONBLOCK) < 0) {
                        close(client_fd);
                        continue;
                    }

                    int one = 1;
                    setsockopt(client_fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));

                    struct epoll_event ev;
                    ev.events = EPOLLIN | EPOLLET;
                    ev.data.fd = client_fd;
                    epoll_ctl(server_config.epoll_fd, EPOLL_CTL_ADD, client_fd, &ev);
                }
            } else {
                int client_fd = events[i].data.fd;
                ConnectionState conn;
                memset(&conn, 0, sizeof(conn));
                conn.fd = client_fd;

                /* Drain all available data (edge-triggered requires EAGAIN loop) */
                ssize_t total = 0;
                while (1) {
                    ssize_t n = read(client_fd, conn.buffer + total, BUFFER_SIZE - 1 - total);
                    if (n < 0) {
                        if (errno == EAGAIN || errno == EWOULDBLOCK) break;
                        if (errno == EINTR) continue;
                        break;
                    }
                    if (n == 0) break;
                    total += n;
                    if (total >= BUFFER_SIZE - 1) break;
                }

                if (total > 0) {
                    conn.buf_len = total;
                    conn.buffer[total] = '\0';

                    if (parse_http_request(&conn) == 0) {
                        handle_request(&conn);
                    }
                }

                epoll_ctl(server_config.epoll_fd, EPOLL_CTL_DEL, client_fd, NULL);
                close(client_fd);
            }
        }
    }

    return NULL;
}

static PyObject *py_serve(PyObject *self, PyObject *args, PyObject *kw) {
    int port;
    const char *root_dir;
    int num_workers = DEFAULT_NUM_WORKERS;
    int cors = 0;
    int caching = 0;
    int etag = 0;
    int no_compression = 0;
    int symlinks = 0;
    int clean_urls = 1;
    int trailing_slash = -1;
    int single = 0;
    PyObject *config_callback = NULL;
    PyObject *custom_headers = NULL;
    PyObject *live_reload = NULL;

    static char *kwlist[] = {"port", "root_dir", "num_workers", "cors", "caching",
                            "etag", "no_compression", "symlinks", "config_callback",
                            "custom_headers", "clean_urls", "trailing_slash", "single",
                            "live_reload", NULL};

    if (!PyArg_ParseTupleAndKeywords(args, kw, "is|iiiiiiOOiiiO", kwlist,
                                     &port, &root_dir, &num_workers, &cors, &caching,
                                     &etag, &no_compression, &symlinks, &config_callback,
                                     &custom_headers, &clean_urls, &trailing_slash, &single,
                                     &live_reload))
        return NULL;

    server_config.epoll_fd = epoll_create1(0);
    if (server_config.epoll_fd < 0) {
        PyErr_SetFromErrno(PyExc_OSError);
        return NULL;
    }

    server_config.listen_fd = socket(AF_INET, SOCK_STREAM, 0);
    if (server_config.listen_fd < 0) {
        close(server_config.epoll_fd);
        PyErr_SetFromErrno(PyExc_OSError);
        return NULL;
    }

    int opt = 1;
    if (setsockopt(server_config.listen_fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt)) < 0) {
        close(server_config.listen_fd);
        close(server_config.epoll_fd);
        PyErr_SetFromErrno(PyExc_OSError);
        return NULL;
    }

    int flags = fcntl(server_config.listen_fd, F_GETFL, 0);
    if (flags < 0 || fcntl(server_config.listen_fd, F_SETFL, flags | O_NONBLOCK) < 0) {
        close(server_config.listen_fd);
        close(server_config.epoll_fd);
        PyErr_SetFromErrno(PyExc_OSError);
        return NULL;
    }

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = INADDR_ANY;
    addr.sin_port = htons(port);

    if (bind(server_config.listen_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        close(server_config.listen_fd);
        close(server_config.epoll_fd);
        PyErr_SetFromErrno(PyExc_OSError);
        return NULL;
    }

    if (listen(server_config.listen_fd, SOMAXCONN) < 0) {
        close(server_config.listen_fd);
        close(server_config.epoll_fd);
        PyErr_SetFromErrno(PyExc_OSError);
        return NULL;
    }

    strncpy(server_config.root_dir, root_dir, PATH_MAX - 1);
    server_config.root_dir[PATH_MAX - 1] = '\0';
    server_config.num_workers = num_workers;
    server_config.running = 1;
    server_config.config_callback = config_callback;
    server_config.etag = etag;
    server_config.cors = cors;
    server_config.caching = caching;
    server_config.no_compression = no_compression;
    server_config.clean_urls = clean_urls;
    server_config.trailing_slash = trailing_slash;
    server_config.single = single;
    server_config.symlinks = symlinks;
    server_config.live_reload = live_reload;

    /* Parse custom headers: list of tuples (pattern, key, value) */
    server_config.header_rules = NULL;
    server_config.num_header_rules = 0;
    if (custom_headers && custom_headers != Py_None && PyList_Check(custom_headers)) {
        Py_ssize_t num_tuples = PyList_Size(custom_headers);
        if (num_tuples > 0) {
            server_config.header_rules = (HeaderRule *)malloc(num_tuples * sizeof(HeaderRule));
            if (!server_config.header_rules) {
                PyErr_NoMemory();
                return NULL;
            }
            server_config.num_header_rules = (int)num_tuples;
            for (Py_ssize_t i = 0; i < num_tuples; i++) {
                PyObject *tuple = PyList_GetItem(custom_headers, i);
                if (!PyTuple_Check(tuple) || PyTuple_Size(tuple) != 3) {
                    PyErr_SetString(PyExc_TypeError, "custom_headers must be list of (pattern, key, value) tuples");
                    return NULL;
                }
                const char *pattern = PyUnicode_AsUTF8(PyTuple_GetItem(tuple, 0));
                const char *key = PyUnicode_AsUTF8(PyTuple_GetItem(tuple, 1));
                const char *value = PyUnicode_AsUTF8(PyTuple_GetItem(tuple, 2));
                if (!pattern || !key) {
                    PyErr_SetString(PyExc_ValueError, "Invalid header tuple");
                    return NULL;
                }
                server_config.header_rules[i].pattern = strdup(pattern);
                server_config.header_rules[i].count = 1;
                server_config.header_rules[i].lines = (char **)malloc(sizeof(char *));
                char line_buf[1024];
                int line_len;
                if (value && value[0]) {
                    line_len = snprintf(line_buf, sizeof(line_buf), "%s: %s", key, value);
                } else {
                    line_len = snprintf(line_buf, sizeof(line_buf), "%s:", key);
                }
                server_config.header_rules[i].lines[0] = strndup(line_buf, line_len);
            }
        }
    }

    struct epoll_event ev;
    ev.events = EPOLLIN | EPOLLET | EPOLLEXCLUSIVE;
    ev.data.fd = server_config.listen_fd;
    epoll_ctl(server_config.epoll_fd, EPOLL_CTL_ADD, server_config.listen_fd, &ev);

    signal(SIGINT, signal_handler);
    signal(SIGTERM, signal_handler);

    server_config.workers = (pthread_t *)malloc(num_workers * sizeof(pthread_t));
    if (!server_config.workers) {
        close(server_config.listen_fd);
        close(server_config.epoll_fd);
        PyErr_NoMemory();
        return NULL;
    }

    for (int i = 0; i < num_workers; i++) {
        if (pthread_create(&server_config.workers[i], NULL, worker_thread, NULL) != 0) {
            server_config.running = 0;
            for (int j = 0; j < i; j++) {
                pthread_join(server_config.workers[j], NULL);
            }
            free(server_config.workers);
            close(server_config.listen_fd);
            close(server_config.epoll_fd);
            PyErr_SetFromErrno(PyExc_OSError);
            return NULL;
        }
    }

    Py_BEGIN_ALLOW_THREADS
    for (int i = 0; i < num_workers; i++) {
        pthread_join(server_config.workers[i], NULL);
    }
    Py_END_ALLOW_THREADS

    free(server_config.workers);
    close(server_config.listen_fd);
    close(server_config.epoll_fd);

    Py_RETURN_NONE;
}

static PyMethodDef methods[] = {
    {"serve", (PyCFunction)py_serve, METH_VARARGS | METH_KEYWORDS, "Start the C HTTP server"},
    {NULL}
};

static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT, "_server",
    "C HTTP server with epoll and thread pool", -1, methods
};

PyMODINIT_FUNC PyInit__server(void) {
    return PyModule_Create(&module);
}
