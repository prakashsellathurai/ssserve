#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <sys/epoll.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/sendfile.h>
#include <netinet/in.h>
#include <arpa/inet.h>
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

#define MAX_EVENTS 64
#define MAX_CONNECTIONS 1024
#define BUFFER_SIZE 8192
#define MAX_PATH 2048
#define SENDFILE_THRESHOLD 65536
#define DEFAULT_NUM_WORKERS 4

typedef struct {
    int epoll_fd;
    int listen_fd;
    char root_dir[PATH_MAX];
    int num_workers;
    pthread_t *workers;
    volatile int running;
    PyObject *config_callback;
    int etag;
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
} ConnectionState;

static ServerConfig server_config;

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
        }
        header = next_header + 2;
    }

    return 0;
}

static void handle_request(ConnectionState *conn) {
    if (conn->method == HTTP_OPTIONS) {
        char response[] =
            "HTTP/1.1 200 OK\r\n"
            "Allow: GET, HEAD, OPTIONS\r\n"
            "Content-Length: 0\r\n"
            "Server: ssserve\r\n"
            "\r\n";
        write_all(conn->fd, response, strlen(response));
        return;
    }

    /* Normalize path: URL decode, collapse slashes, resolve . and .., security checks */
    char normalized[MAX_PATH];
    if (normalize_path(conn->path, normalized, sizeof(normalized)) < 0) {
        char response[] = "HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n";
        write_all(conn->fd, response, strlen(response));
        return;
    }

    char full_path[PATH_MAX];
    if (resolve_path(normalized, server_config.root_dir, full_path, sizeof(full_path)) < 0) {
        char response[] = "HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n";
        write_all(conn->fd, response, strlen(response));
        return;
    }

    struct stat st;
    if (stat(full_path, &st) < 0) {
        char not_found_path[PATH_MAX];
        snprintf(not_found_path, sizeof(not_found_path), "%s/404.html", server_config.root_dir);
        if (stat(not_found_path, &st) == 0) {
            int file_fd = open(not_found_path, O_RDONLY);
            if (file_fd >= 0) {
                char header_buf[512];
                int header_len = snprintf(header_buf, sizeof(header_buf),
                    "HTTP/1.1 404 Not Found\r\n"
                    "Content-Type: text/html\r\n"
                    "Content-Length: %ld\r\n"
                    "Server: ssserve\r\n"
                    "\r\n",
                    (long)st.st_size);
                write_all(conn->fd, header_buf, header_len);

                if (st.st_size > SENDFILE_THRESHOLD) {
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
                    char file_buf[BUFFER_SIZE];
                    ssize_t bytes_read;
                    while ((bytes_read = read(file_fd, file_buf, sizeof(file_buf))) > 0) {
                        if (write_all(conn->fd, file_buf, bytes_read) < 0) break;
                    }
                }
                close(file_fd);
                return;
            }
        }
        char response[] = "HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n";
        write_all(conn->fd, response, strlen(response));
        return;
    }

    if (S_ISDIR(st.st_mode)) {
        char index_path[PATH_MAX];
        snprintf(index_path, sizeof(index_path), "%s/index.html", full_path);
        if (stat(index_path, &st) < 0) {
            char response[] = "HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n";
            write_all(conn->fd, response, strlen(response));
            return;
        }
        snprintf(full_path, sizeof(full_path), "%s", index_path);
    }

    const char *mime_type = get_mime_type(full_path);
    char header_buf[512];
    int header_len;

    int etag_enabled = server_config.etag;
    char etag_buf[64] = {0};
    if (etag_enabled) {
        compute_etag((long)st.st_mtime, (long)st.st_size, etag_buf, sizeof(etag_buf));
    }

    /* 304 Not Modified: If-None-Match */
    if (etag_enabled && conn->if_none_match) {
        if (strcmp(conn->if_none_match, etag_buf) == 0) {
            header_len = snprintf(header_buf, sizeof(header_buf),
                "HTTP/1.1 304 Not Modified\r\n"
                "ETag: %s\r\n"
                "Server: ssserve\r\n"
                "\r\n",
                etag_buf);
            write_all(conn->fd, header_buf, header_len);
            return;
        }
    }

    /* 304 Not Modified: If-Modified-Since (when ETag disabled) */
    if (!etag_enabled && conn->if_modified_since) {
        struct tm ims_tm;
        memset(&ims_tm, 0, sizeof(ims_tm));
        if (strptime(conn->if_modified_since, "%a, %d %b %Y %H:%M:%S", &ims_tm) != NULL) {
            time_t ims_time = timegm(&ims_tm);
            if ((long)st.st_mtime <= (long)ims_time) {
                header_len = snprintf(header_buf, sizeof(header_buf),
                    "HTTP/1.1 304 Not Modified\r\n"
                    "Server: ssserve\r\n"
                    "\r\n");
                write_all(conn->fd, header_buf, header_len);
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
            "\r\n",
            mime_type, (long)st.st_size,
            etag_enabled ? "ETag: " : "Last-Modified: ",
            etag_enabled ? etag_buf : "");
        write_all(conn->fd, header_buf, header_len);
        return;
    }

    /* Check if gzip is acceptable */
    int use_gzip = 0;
    if (conn->accept_encoding && strstr(conn->accept_encoding, "gzip")) {
        use_gzip = 1;
    }

    int file_fd = open(full_path, O_RDONLY);
    if (file_fd < 0) {
        char response[] = "HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\n\r\n";
        write_all(conn->fd, response, strlen(response));
        return;
    }

    /* For gzip: read file into memory, compress, and send */
    if (use_gzip && st.st_size < 256 * 1024 && st.st_size > 0) {
        char *file_data = (char *)malloc(st.st_size);
        if (file_data) {
            ssize_t total_read = 0;
            while (total_read < st.st_size) {
                ssize_t n = read(file_fd, file_data + total_read, st.st_size - total_read);
                if (n <= 0) break;
                total_read += n;
            }
            close(file_fd);

            size_t compressed_len = 0;
            char *compressed = gzip_compress(file_data, total_read, &compressed_len, 6);
            free(file_data);

            if (compressed) {
                header_len = snprintf(header_buf, sizeof(header_buf),
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: %s\r\n"
                    "Content-Length: %zu\r\n"
                    "Content-Encoding: gzip\r\n"
                    "Server: ssserve\r\n"
                    "%s%s\r\n"
                    "\r\n",
                    mime_type, compressed_len,
                    etag_enabled ? "ETag: " : "Last-Modified: ",
                    etag_enabled ? etag_buf : "");
                write_all(conn->fd, header_buf, header_len);
                write_all(conn->fd, compressed, compressed_len);
                free(compressed);
                return;
            }
            /* gzip failed, fall through to normal send */
            file_fd = open(full_path, O_RDONLY);
            if (file_fd < 0) {
                char response[] = "HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\n\r\n";
                write_all(conn->fd, response, strlen(response));
                return;
            }
        }
    }

    header_len = snprintf(header_buf, sizeof(header_buf),
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: %s\r\n"
        "Content-Length: %ld\r\n"
        "Server: ssserve\r\n"
        "%s%s\r\n"
        "\r\n",
        mime_type, (long)st.st_size,
        etag_enabled ? "ETag: " : "Last-Modified: ",
        etag_enabled ? etag_buf : "");
    write_all(conn->fd, header_buf, header_len);

    if (st.st_size > SENDFILE_THRESHOLD) {
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
        char file_buf[BUFFER_SIZE];
        ssize_t bytes_read;
        while ((bytes_read = read(file_fd, file_buf, sizeof(file_buf))) > 0) {
            if (write_all(conn->fd, file_buf, bytes_read) < 0) break;
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
    PyObject *config_callback = NULL;

    static char *kwlist[] = {"port", "root_dir", "num_workers", "cors", "caching",
                            "etag", "no_compression", "symlinks", "config_callback", NULL};

    if (!PyArg_ParseTupleAndKeywords(args, kw, "is|iiiiiiO", kwlist,
                                     &port, &root_dir, &num_workers, &cors, &caching,
                                     &etag, &no_compression, &symlinks, &config_callback))
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

    if (listen(server_config.listen_fd, 128) < 0) {
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
