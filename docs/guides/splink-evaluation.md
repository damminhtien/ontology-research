# Splink resolver evaluation

Đây là benchmark offline để đánh giá Splink 5 như một bộ đề xuất. Resolver,
identity store, event log, FastAPI và read model đang chạy không được sửa hay
ghi bởi thí nghiệm. Resolver baseline dùng một `IdentityService` in-memory
tạm thời.

## Tái lập

```bash
make splink-fetch
make splink-test
make splink-eval
```

`splink-fetch` tải FEBRL4 từ revision được ghim trong
`benchmarks/evaluations/febrl4_sources.json` và xác minh SHA-256 của cả hai
file. Dữ liệu chỉ nằm trong `build/splink-evaluation/datasets/`. `splink-eval`
không truy cập mạng và dừng nếu file thiếu hoặc checksum không khớp.

Môi trường riêng dùng Python 3.13, `splink==5.0.0`, `duckdb==2.0.0` và
`psutil==7.2.2`; dependency này không được thêm vào runtime của ứng dụng.
Lệnh có thể chạy trực tiếp với cấu hình khác:

```bash
uv run --no-project --python 3.13 \
  --with-requirements requirements/evaluation-splink.txt \
  python tools/evaluate_splink.py \
  --synthetic-entities 900 --seed 42 --max-pairs 100000 \
  --output build/splink-evaluation/report.json
```

## Protocol

Benchmark chia theo thực thể thật thành train/validation/test theo tỷ lệ
60/20/20, seed 42. Splink chỉ học tham số không giám sát trên train. Ngưỡng
được chọn bằng F1 pairwise trên validation rồi khóa trước khi đọc kết quả test.
Mỗi phương pháp có cùng ngân sách review: tối đa năm đề xuất cho mỗi bản ghi
nguồn.

Benchmark dừng nếu model còn thiếu ước lượng `m` hoặc `u` cho bất kỳ comparison
level khác null nào. Với FEBRL4, `address_1` chỉ tham gia blocking vì train split
không có đủ quan sát cho một level tên gần khớp ổn định.

Synthetic data có tên đa ngôn ngữ và bí danh, lỗi chính tả, tên trùng giữa
thực thể, trusted ID bị thiếu, nhiều loại thực thể và cặp `cannot-link`.
FEBRL4 dùng hai file 5.000 dòng của Splink. Ground-truth ID được phân tích
riêng để chia và chấm điểm. `rec_id`, nhãn cụm và `soc_sec_id` không đi vào
Splink; `soc_sec_id` chỉ được cấp cho baseline vì resolver hiện tại hỗ trợ
lookup trusted ID.

Các phương pháp được so sánh:

1. Resolver hiện tại: exact trusted-ID/alias lookup, sau đó fuzzy token overlap
   theo ngưỡng review hiện hành; danh sách review giữ thứ tự ID như resolver.
2. Splink rerank: chấm chính xác cùng tập cặp mà resolver tạo.
3. Splink blocking: tự sinh tập candidate bằng rule trong profile của dataset.

Mọi candidate qua cùng safety filter trước khi thành đề xuất. Cặp sai loại và
cặp `cannot-link` bị loại. Báo cáo gồm candidate recall, recall@5, pairwise
precision/recall, số đề xuất sai, lỗi do blocking/ranking/thiếu dữ liệu, thời
gian từng giai đoạn và peak RSS được lấy mẫu. Báo cáo không chứa bản ghi hoặc
danh sách cặp; file sinh ra nằm trong `build/` và không được commit.

## Đọc kết quả

Chạy `make splink-eval` hai lần với cùng seed để kiểm tra metric và tập candidate
có lặp lại; thời gian và RSS có thể thay đổi theo tải máy. Chỉ cân nhắc một
thử nghiệm review nếu recall tăng mà precision không giảm tại cùng ngân sách.
Kết quả benchmark không kích hoạt merge/split event và không ghi dữ liệu vào
identity store.
