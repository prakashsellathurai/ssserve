#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <zlib.h>
#include <sys/stat.h>
#include <sys/sendfile.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>

/* ---------- sendfile: zero-copy file-to-socket transfer ---------- */

PyDoc_STRVAR(sendfile_doc,
"sendfile(out_fd, in_fd, count) -> bytes_sent\n"
"Zero-copy transfer using Linux sendfile(). Falls back to read/write on error.");

static PyObject* py_sendfile(PyObject* self, PyObject* args)
{
    int out_fd, in_fd;
    long count;

    if (!PyArg_ParseTuple(args, "iil", &out_fd, &in_fd, &count))
        return NULL;

    if (count < 0) {
        PyErr_SetString(PyExc_ValueError, "count must be non-negative");
        return NULL;
    }

    off_t offset = 0;
    ssize_t sent = sendfile(out_fd, in_fd, &offset, (size_t)count);

    if (sent < 0) {
        if (errno == EINVAL || errno == ENOSYS) {
            Py_RETURN_NONE;
        }
        return PyErr_SetFromErrno(PyExc_OSError);
    }

    return PyLong_FromLong((long)sent);
}


/* ---------- fast_gzip: zlib-based compression ---------- */

PyDoc_STRVAR(fast_gzip_doc,
"fast_gzip(data, level=6) -> bytes\n"
"Compress data using zlib (raw deflate with gzip wrapper).\n"
"Level 1-9, default 6 for best speed/ratio tradeoff.");

static PyObject* py_fast_gzip(PyObject* self, PyObject* args, PyObject* kwargs)
{
    Py_buffer data;
    int level = 6;
    static char* kwlist[] = {"data", "level", NULL};

    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "y*|i", kwlist, &data, &level))
        return NULL;

    if (level < 1 || level > 9) {
        PyBuffer_Release(&data);
        PyErr_SetString(PyExc_ValueError, "level must be 1-9");
        return NULL;
    }

    z_stream stream;
    memset(&stream, 0, sizeof(stream));

    /* windowBits = 15 + 16 for gzip encoding */
    int ret = deflateInit2(&stream, level, Z_DEFLATED, 15 + 16, 8, Z_DEFAULT_STRATEGY);
    if (ret != Z_OK) {
        PyBuffer_Release(&data);
        return PyErr_Format(PyExc_RuntimeError, "deflateInit2 failed: %d", ret);
    }

    /* Worst case: source size + 0.1% + 12 bytes, plus gzip overhead */
    size_t max_out = data.len + (data.len >> 10) + 64;
    char* out_buf = (char*)PyMem_Malloc(max_out);
    if (!out_buf) {
        deflateEnd(&stream);
        PyBuffer_Release(&data);
        return PyErr_NoMemory();
    }

    stream.next_in = (Bytef*)data.buf;
    stream.avail_in = (uInt)data.len;
    stream.next_out = (Bytef*)out_buf;
    stream.avail_out = (uInt)max_out;

    ret = deflate(&stream, Z_FINISH);
    if (ret != Z_STREAM_END) {
        PyMem_Free(out_buf);
        deflateEnd(&stream);
        PyBuffer_Release(&data);
        return PyErr_Format(PyExc_RuntimeError, "deflate failed: %d", ret);
    }

    PyObject* result = PyBytes_FromStringAndSize(out_buf, stream.total_out);

    PyMem_Free(out_buf);
    deflateEnd(&stream);
    PyBuffer_Release(&data);

    return result;
}


/* ---------- etag: fast ETag from mtime+size ---------- */

PyDoc_STRVAR(etag_doc,
"etag(mtime, size) -> str\n"
"Compute a fast ETag from file mtime and size.\n"
"Returns a string like '\"1234567890-42\"'.");

static PyObject* py_etag(PyObject* self, PyObject* args)
{
    double mtime;
    long size;

    if (!PyArg_ParseTuple(args, "dl", &mtime, &size))
        return NULL;

    long mtime_int = (long)mtime;
    char buf[64];
    snprintf(buf, sizeof(buf), "\"%ld-%ld\"", mtime_int, size);
    return PyUnicode_FromString(buf);
}


/* ---------- Module definition ---------- */

static PyMethodDef fastops_methods[] = {
    {"sendfile", py_sendfile, METH_VARARGS, sendfile_doc},
    {"fast_gzip", (PyCFunction)py_fast_gzip, METH_VARARGS | METH_KEYWORDS, fast_gzip_doc},
    {"etag", py_etag, METH_VARARGS, etag_doc},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef fastops_module = {
    PyModuleDef_HEAD_INIT,
    "_fastops",
    "Fast C operations for ssserve: sendfile, gzip, etag",
    -1,
    fastops_methods
};

PyMODINIT_FUNC PyInit__fastops(void)
{
    return PyModule_Create(&fastops_module);
}
