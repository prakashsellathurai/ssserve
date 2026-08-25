#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <zlib.h>
#include <sys/stat.h>
#include <sys/sendfile.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <string.h>
#include <time.h>

/* ================================================================
 *  CCache — LRU+TTL cache with hash table in C
 * ================================================================ */

typedef struct {
    char *key;
    PyObject *value;
    double ts;
} CEntry;

typedef struct {
    PyObject_HEAD
    CEntry *entries;
    int *ht;
    int ht_cap;
    int cap;
    int count;
    int *prev;
    int *next;
    int head;
    int tail;
    double ttl;
    unsigned long hits;
    unsigned long misses;
    PyThread_type_lock lock;
} CCache;

static unsigned int fnv1a(const char *s) {
    unsigned int h = 2166136261u;
    while (*s) { h ^= (unsigned char)*s++; h *= 16777619u; }
    return h;
}

static int ht_find(CCache *c, const char *key) {
    unsigned int h = fnv1a(key) % (unsigned int)c->ht_cap;
    for (int i = 0; i < c->ht_cap; i++) {
        int idx = c->ht[h];
        if (idx < 0) return -1;
        if (c->entries[idx].key && strcmp(c->entries[idx].key, key) == 0) return idx;
        h = (h + 1) % (unsigned int)c->ht_cap;
    }
    return -1;
}

static void ht_insert(CCache *c, const char *key, int idx) {
    unsigned int h = fnv1a(key) % (unsigned int)c->ht_cap;
    for (int i = 0; i < c->ht_cap; i++) {
        if (c->ht[h] < 0) { c->ht[h] = idx; return; }
        h = (h + 1) % (unsigned int)c->ht_cap;
    }
}

static void ht_remove(CCache *c, const char *key) {
    unsigned int h = fnv1a(key) % (unsigned int)c->ht_cap;
    for (int i = 0; i < c->ht_cap; i++) {
        int idx = c->ht[h];
        if (idx < 0) return;
        if (c->entries[idx].key && strcmp(c->entries[idx].key, key) == 0) {
            c->ht[h] = -1;
            int gap = (int)h;
            for (int j = (int)((h + 1) % (unsigned int)c->ht_cap); ; j = (int)((j + 1) % (unsigned int)c->ht_cap)) {
                if (c->ht[j] < 0) break;
                const char *k2 = c->entries[c->ht[j]].key;
                int h2 = (int)(fnv1a(k2) % (unsigned int)c->ht_cap);
                int d_cur = (j >= h2) ? j - h2 : c->ht_cap - h2 + j;
                int d_gap = (gap >= h2) ? gap - h2 : c->ht_cap - h2 + gap;
                if (d_cur < d_gap) { c->ht[gap] = c->ht[j]; c->ht[j] = -1; gap = j; }
            }
            return;
        }
        h = (h + 1) % (unsigned int)c->ht_cap;
    }
}

static void ll_unlink(CCache *c, int idx) {
    int p = c->prev[idx], n = c->next[idx];
    if (p >= 0) c->next[p] = n; else c->head = n;
    if (n >= 0) c->prev[n] = p; else c->tail = p;
    c->prev[idx] = -1; c->next[idx] = -1;
}

static void ll_link_tail(CCache *c, int idx) {
    c->prev[idx] = c->tail; c->next[idx] = -1;
    if (c->tail >= 0) c->next[c->tail] = idx; else c->head = idx;
    c->tail = idx;
}

static double now_mono(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

static void ccache_acquire(CCache *c) {
    PyThread_acquire_lock(c->lock, 0);
}
static void ccache_release(CCache *c) {
    PyThread_release_lock(c->lock);
}

static int CCache_init(CCache *self, PyObject *args, PyObject *kw) {
    int max_size = 1024;
    double ttl = 60.0;
    static char *kwlist[] = {"max_size", "ttl", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kw, "|id", kwlist, &max_size, &ttl))
        return -1;
    self->cap = max_size; self->ttl = ttl; self->count = 0;
    self->head = -1; self->tail = -1; self->hits = 0; self->misses = 0;
    self->entries = (CEntry *)PyMem_Calloc(max_size, sizeof(CEntry));
    self->prev = (int *)PyMem_Malloc(sizeof(int) * max_size);
    self->next = (int *)PyMem_Malloc(sizeof(int) * max_size);
    self->ht_cap = max_size * 2;
    self->ht = (int *)PyMem_Malloc(sizeof(int) * self->ht_cap);
    for (int i = 0; i < self->ht_cap; i++) self->ht[i] = -1;
    for (int i = 0; i < max_size; i++) { self->prev[i] = -1; self->next[i] = -1; }
    self->lock = PyThread_allocate_lock();
    if (!self->lock) return -1;
    return 0;
}

static void CCache_dealloc(CCache *self) {
    if (self->entries) {
        for (int i = 0; i < self->cap; i++) {
            if (self->entries[i].key) PyMem_Free(self->entries[i].key);
            Py_XDECREF(self->entries[i].value);
        }
        PyMem_Free(self->entries);
    }
    PyMem_Free(self->ht);
    PyMem_Free(self->prev);
    PyMem_Free(self->next);
    if (self->lock) PyThread_free_lock(self->lock);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static PyObject *CCache_get(CCache *self, PyObject *args) {
    const char *key;
    if (!PyArg_ParseTuple(args, "s", &key)) return NULL;
    ccache_acquire(self);
    int idx = ht_find(self, key);
    if (idx < 0) { self->misses++; ccache_release(self); Py_RETURN_NONE; }
    double now = now_mono();
    if (now - self->entries[idx].ts > self->ttl) {
        ht_remove(self, key);
        ll_unlink(self, idx);
        if (self->entries[idx].key) { PyMem_Free(self->entries[idx].key); self->entries[idx].key = NULL; }
        Py_XDECREF(self->entries[idx].value); self->entries[idx].value = NULL;
        self->count--; self->misses++;
        ccache_release(self); Py_RETURN_NONE;
    }
    self->hits++;
    if ((self->hits & 63) == 0) { ll_unlink(self, idx); ll_link_tail(self, idx); }
    PyObject *val = self->entries[idx].value;
    Py_INCREF(val);
    ccache_release(self);
    return val;
}

static PyObject *CCache_set(CCache *self, PyObject *args) {
    const char *key;
    PyObject *value;
    if (!PyArg_ParseTuple(args, "sO", &key, &value)) return NULL;
    double now = now_mono();
    Py_INCREF(value);
    ccache_acquire(self);
    int idx = ht_find(self, key);
    if (idx >= 0) {
        Py_DECREF(self->entries[idx].value);
        self->entries[idx].value = value;
        self->entries[idx].ts = now;
        ll_unlink(self, idx); ll_link_tail(self, idx);
    } else {
        idx = -1;
        for (int i = 0; i < self->cap; i++) { if (!self->entries[i].key) { idx = i; break; } }
        if (idx < 0) {
            idx = self->head;
            if (idx >= 0) {
                ht_remove(self, self->entries[idx].key);
                PyMem_Free(self->entries[idx].key); self->entries[idx].key = NULL;
                Py_DECREF(self->entries[idx].value); self->entries[idx].value = NULL;
                ll_unlink(self, idx); self->count--;
            }
        }
        self->entries[idx].key = (char *)PyMem_Malloc(strlen(key) + 1);
        strcpy(self->entries[idx].key, key);
        self->entries[idx].value = value;
        self->entries[idx].ts = now;
        ht_insert(self, key, idx);
        ll_link_tail(self, idx);
        self->count++;
    }
    ccache_release(self);
    Py_RETURN_NONE;
}

static PyObject *CCache_clear(CCache *self, PyObject *Py_UNUSED(ignored)) {
    ccache_acquire(self);
    for (int i = 0; i < self->cap; i++) {
        if (self->entries[i].key) { PyMem_Free(self->entries[i].key); self->entries[i].key = NULL; }
        Py_XDECREF(self->entries[i].value); self->entries[i].value = NULL;
    }
    for (int i = 0; i < self->ht_cap; i++) self->ht[i] = -1;
    self->count = 0; self->head = -1; self->tail = -1;
    for (int i = 0; i < self->cap; i++) { self->prev[i] = -1; self->next[i] = -1; }
    ccache_release(self);
    Py_RETURN_NONE;
}

static PyObject *CCache_stats(CCache *self, PyObject *Py_UNUSED(ignored)) {
    return Py_BuildValue("{sksksk}", "hits", self->hits, "misses", self->misses, "size", self->count);
}

static PyMethodDef CCache_methods[] = {
    {"get", (PyCFunction)CCache_get, METH_VARARGS, NULL},
    {"set", (PyCFunction)CCache_set, METH_VARARGS, NULL},
    {"clear", (PyCFunction)CCache_clear, METH_NOARGS, NULL},
    {"stats", (PyCFunction)CCache_stats, METH_NOARGS, NULL},
    {NULL}
};

static PyTypeObject CCacheType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "_fastops.CCache",
    .tp_basicsize = sizeof(CCache),
    .tp_dealloc = (destructor)CCache_dealloc,
    .tp_init = (initproc)CCache_init,
    .tp_new = PyType_GenericNew,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_methods = CCache_methods,
};

/* ================================================================
 *  ETag
 * ================================================================ */
static PyObject *py_etag(PyObject *self, PyObject *args) {
    double mtime; long size;
    if (!PyArg_ParseTuple(args, "dl", &mtime, &size)) return NULL;
    char buf[64];
    snprintf(buf, sizeof(buf), "\"%ld-%ld\"", (long)mtime, size);
    return PyUnicode_FromString(buf);
}

/* ================================================================
 *  Gzip
 * ================================================================ */
static PyObject *py_gzip(PyObject *self, PyObject *args, PyObject *kw) {
    Py_buffer data; int level = 6;
    static char *kwlist[] = {"data", "level", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kw, "y*|i", kwlist, &data, &level))
        return NULL;
    z_stream s; memset(&s, 0, sizeof(s));
    int r = deflateInit2(&s, level, Z_DEFLATED, 15+16, 8, Z_DEFAULT_STRATEGY);
    if (r != Z_OK) { PyBuffer_Release(&data); return PyErr_Format(PyExc_RuntimeError, "deflateInit2: %d", r); }
    size_t mx = data.len + (data.len >> 10) + 64;
    char *out = (char *)PyMem_Malloc(mx);
    if (!out) { deflateEnd(&s); PyBuffer_Release(&data); return PyErr_NoMemory(); }
    s.next_in = (Bytef *)data.buf; s.avail_in = (uInt)data.len;
    s.next_out = (Bytef *)out; s.avail_out = (uInt)mx;
    r = deflate(&s, Z_FINISH);
    if (r != Z_STREAM_END) { PyMem_Free(out); deflateEnd(&s); PyBuffer_Release(&data); return PyErr_Format(PyExc_RuntimeError, "deflate: %d", r); }
    PyObject *result = PyBytes_FromStringAndSize(out, s.total_out);
    PyMem_Free(out); deflateEnd(&s); PyBuffer_Release(&data);
    return result;
}

/* ================================================================
 *  sendfile
 * ================================================================ */
static PyObject *py_sendfile(PyObject *self, PyObject *args) {
    int out_fd, in_fd; long count;
    if (!PyArg_ParseTuple(args, "iil", &out_fd, &in_fd, &count)) return NULL;
    off_t offset = 0;
    ssize_t sent = sendfile(out_fd, in_fd, &offset, (size_t)count);
    if (sent < 0) { if (errno == EINVAL || errno == ENOSYS) Py_RETURN_NONE; return PyErr_SetFromErrno(PyExc_OSError); }
    return PyLong_FromLong((long)sent);
}

/* ================================================================
 *  MIME — inline lookup for common types
 * ================================================================ */
static PyObject *py_guess_type(PyObject *self, PyObject *args) {
    const char *path;
    if (!PyArg_ParseTuple(args, "s", &path)) return NULL;
    const char *dot = strrchr(path, '.');
    if (!dot) return PyUnicode_FromString("application/octet-stream");
    const char *ext = dot + 1;
    /* Fast inline for top web types */
    switch (ext[0]) {
    case 'c': if (strcmp(ext,"css")==0) return PyUnicode_FromString("text/css"); if (strcmp(ext,"csv")==0) return PyUnicode_FromString("text/csv"); break;
    case 'g': if (strcmp(ext,"gif")==0) return PyUnicode_FromString("image/gif"); if (strcmp(ext,"gz")==0) return PyUnicode_FromString("application/gzip"); break;
    case 'h': if (strcmp(ext,"html")==0||strcmp(ext,"htm")==0) return PyUnicode_FromString("text/html"); break;
    case 'j':
        if (strcmp(ext,"js")==0||strcmp(ext,"mjs")==0) return PyUnicode_FromString("application/javascript");
        if (strcmp(ext,"json")==0) return PyUnicode_FromString("application/json");
        if (strcmp(ext,"jpeg")==0||strcmp(ext,"jpg")==0) return PyUnicode_FromString("image/jpeg");
        break;
    case 'm': if (strcmp(ext,"mp4")==0) return PyUnicode_FromString("video/mp4"); if (strcmp(ext,"map")==0) return PyUnicode_FromString("application/json"); break;
    case 'p': if (strcmp(ext,"png")==0) return PyUnicode_FromString("image/png"); if (strcmp(ext,"pdf")==0) return PyUnicode_FromString("application/pdf"); break;
    case 's': if (strcmp(ext,"svg")==0) return PyUnicode_FromString("image/svg+xml"); break;
    case 't': if (strcmp(ext,"txt")==0) return PyUnicode_FromString("text/plain"); if (strcmp(ext,"ts")==0) return PyUnicode_FromString("video/mp2t"); break;
    case 'w':
        if (strcmp(ext,"wasm")==0) return PyUnicode_FromString("application/wasm");
        if (strcmp(ext,"webm")==0) return PyUnicode_FromString("video/webm");
        if (strcmp(ext,"webp")==0) return PyUnicode_FromString("image/webp");
        if (strcmp(ext,"woff")==0) return PyUnicode_FromString("font/woff");
        if (strcmp(ext,"woff2")==0) return PyUnicode_FromString("font/woff2");
        break;
    case 'x': if (strcmp(ext,"xml")==0) return PyUnicode_FromString("application/xml"); break;
    case 'z': if (strcmp(ext,"zip")==0) return PyUnicode_FromString("application/zip"); break;
    case 'a': if (strcmp(ext,"avif")==0) return PyUnicode_FromString("image/avif"); break;
    }
    return PyUnicode_FromString("application/octet-stream");
}

/* ================================================================
 *  Module
 * ================================================================ */
static PyMethodDef methods[] = {
    {"etag", py_etag, METH_VARARGS, NULL},
    {"fast_gzip", (PyCFunction)py_gzip, METH_VARARGS|METH_KEYWORDS, NULL},
    {"sendfile", py_sendfile, METH_VARARGS, NULL},
    {"guess_type", py_guess_type, METH_VARARGS, NULL},
    {NULL}
};

static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT, "_fastops",
    "C extensions: CCache, etag, gzip, sendfile, guess_type", -1, methods
};

PyMODINIT_FUNC PyInit__fastops(void) {
    PyObject *m;
    if (PyType_Ready(&CCacheType) < 0) return NULL;
    m = PyModule_Create(&module);
    if (!m) return NULL;
    Py_INCREF(&CCacheType);
    PyModule_AddObject(m, "CCache", (PyObject *)&CCacheType);
    return m;
}
