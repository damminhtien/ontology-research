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
Splink; `soc_sec_id` chỉ được cấp cho baseline production vì resolver hiện tại
hỗ trợ lookup trusted ID.

Kết quả production và controlled comparison được báo cáo tách biệt. Production
comparison giữ nguyên baseline thật: resolver dùng trusted ID và cutoff fuzzy
0.50; Splink không có ID, tự chọn threshold trên validation. Vì candidate set
của resolver có thể được `soc_sec_id` hỗ trợ, đây là so sánh hành vi production,
không phải phép so sánh thuật toán cùng điều kiện.

Controlled comparison trên FEBRL4 chỉ đưa normalized combined name vào cả hai
phương pháp, loại `soc_sec_id` và các trường khác. Cùng train/validation/test,
threshold F1 trên validation và giới hạn năm đề xuất được dùng. Resolver giữ
mọi candidate có overlap dương để threshold 0.50 không cắt mất dữ liệu trước
khi tune. Splink chạy một profile name-only với m prior được ghi trong JSON;
u được ước lượng từ cặp ngẫu nhiên thuộc train. EM không chạy cho profile này
vì exact-name block không chứa các fuzzy comparison level để ước lượng.

Các phương pháp production comparison:

1. Resolver hiện tại: exact trusted-ID/alias lookup, sau đó fuzzy token overlap
   theo ngưỡng review hiện hành; danh sách review giữ thứ tự ID như resolver.
2. Splink rerank: chấm chính xác cùng tập cặp mà resolver tạo.
3. Splink blocking: tự sinh tập candidate bằng rule trong profile của dataset.

Controlled comparison thêm resolver name-only, Splink name-only rerank trên cùng
tập candidate và Splink name-only blocking độc lập. Splink đa đặc trưng được giữ
riêng trong production comparison để đánh giá lợi ích của các trường FEBRL4.

Mọi phương pháp dùng cùng safety filter trước khi tạo đề xuất. Cặp sai loại và
cặp `cannot-link` bị loại. Báo cáo gồm candidate recall, recall@5, pairwise
precision/recall, số đề xuất sai, lỗi do blocking/ranking/thiếu dữ liệu, thời
gian từng giai đoạn và peak RSS được lấy mẫu. JSON có `report_version`, phiên
bản môi trường, hash dataset và lệnh tái lập; snapshot tham chiếu đã đo được
lưu ở `benchmarks/evaluations/reports/splink-v2.json`. Lệnh mặc định vẫn ghi
report mới vào `build/`.

## Đọc kết quả

Chạy `make splink-eval` hai lần với cùng seed để kiểm tra metric và tập candidate
có lặp lại; thời gian và RSS có thể thay đổi theo tải máy. Kết quả FEBRL4 hiện
không cho thấy Splink name-only blocking cải thiện recall của resolver. Name-only
rerank tăng precision và recall trên test ở cùng trần review; chỉ nên đề xuất
thử nghiệm review nếu precision không giảm tại cùng ngân sách. Kết quả benchmark
không kích hoạt merge/split event và không ghi dữ liệu vào identity store.
