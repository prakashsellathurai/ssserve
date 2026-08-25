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
} ServerConfig;

typedef struct {
    int fd;
    char buffer[BUFFER_SIZE];
    int buf_len;
    int method;
    char path[MAX_PATH];
    char *host;
    /* Headers parsed in Tasks 3, 6: Accept-Encoding, Range, If-None-Match, If-Modified-Since */
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
        if (strncmp(header, "Host:", 5) == 0) {
            conn->host = header + 5; /* Points into conn->buffer, valid for conn lifetime */
            while (*conn->host == ' ') conn->host++;
            char *end = strstr(conn->host, "\r\n");
            if (end) *end = '\0';
        }
        char *next_header = strstr(header, "\r\n");
        if (!next_header) break;
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

    char full_path[PATH_MAX];
    if (strcmp(conn->path, "/") == 0) {
        snprintf(full_path, sizeof(full_path), "%s/index.html", server_config.root_dir);
    } else {
        snprintf(full_path, sizeof(full_path), "%s%s", server_config.root_dir, conn->path);
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

    if (conn->method == HTTP_HEAD) {
        header_len = snprintf(header_buf, sizeof(header_buf),
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: %s\r\n"
            "Content-Length: %ld\r\n"
            "Server: ssserve\r\n"
            "\r\n",
            mime_type, (long)st.st_size);
        write_all(conn->fd, header_buf, header_len);
        return;
    }

    int file_fd = open(full_path, O_RDONLY);
    if (file_fd < 0) {
        char response[] = "HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\n\r\n";
        write_all(conn->fd, response, strlen(response));
        return;
    }

    header_len = snprintf(header_buf, sizeof(header_buf),
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: %s\r\n"
        "Content-Length: %ld\r\n"
        "Server: ssserve\r\n"
        "\r\n",
        mime_type, (long)st.st_size);
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
    server_config.config_callback = config_callback; /* Used in Task 5 for redirects/rewrites */

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
