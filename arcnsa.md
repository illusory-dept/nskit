# arc.nsa

## 1. Structure

```
Header
Directory
Data Block
```

---

## 2. Header - 6 (or 7) bytes

### Optional pad - 1 byte?

Uncommon. Present in some titles.

### `object_count` (16-bit, big-endian) - 2 bytes

Number of directory entries.

### `global_data_base_offset` (32-bit, big-endian) - 4 bytes

Absolute position in file where the packed data block begins.

---

## 3. Directory (Repeated `object_count` times)

Each entry contains:

### 1. `file_name`

- Null-terminated ASCII string (may include backslashes).

### 2. `compression_flag` – 1 byte:

| Flag | Type         |
| ---- | ------------ |
| 0    | None         |
| 1    | SPB (Image)  |
| 2    | LZSS (Image) |
| 4    | NBZ (Audio)  |

### 3. `rel_offset` - 4 bytes (big-endian)

Start of file data relative to global_data_base_offset.

### 4. `stored_size` - 4 bytes (big-endian)

Number of bytes stored in archive for this file.

### 5. `expanded_size` - 4 bytes (big-endian)

Expected size after decompression.
May be 0 for SPB or NBZ.

---

## 4. Data Block

- Begins at global_data_base_offset.
- Contains raw bytes or compressed data.
- Runs to end of archive.

For each file:

```
	start = global_data_base_offset + rel_offset
	length = stored_size
```

---
