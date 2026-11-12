# Đánh giá chất lượng ontology

`make quality` chạy ngoại tuyến trên ontology, SHACL shapes và dữ liệu mẫu
trong repo. Kết quả ghi vào `build/ontology-quality.json`. `make check` chạy
cùng bộ đánh giá và GitHub Actions lưu artifact `ontology-quality`, kể cả khi
có lỗi chất lượng và báo cáo đã được tạo.

```bash
make setup                   # lần đầu
make quality                 # ghi báo cáo JSON và in PASS/FAIL
jq '{schema, shacl, competency}' build/ontology-quality.json
.venv/bin/python tools/check_ontology_quality.py  # JSON ra stdout
```

Mã thoát: `0` nếu đạt các điều kiện bắt buộc, `1` nếu báo cáo có lỗi chất
lượng, `2` nếu không thể chạy đánh giá (ví dụ file Turtle không đọc được).
Dùng cùng commit và phiên bản thư viện để tái lập báo cáo. Báo cáo không chứa
thời gian chạy; blank node được hiển thị chung là `_:blank`.

## Chọn cách đo

[Grüninger và Fox](https://eil.utoronto.ca/wp-content/uploads/enterprise-modelling/papers/gruninger-onto-ecai94.pdf)
dùng competency questions (CQ) để kiểm tra ontology đáp ứng công việc đã định
nghĩa. Repo triển khai cách này bằng SPARQL trên đồ thị mẫu và so sánh kết quả
với đáp án lưu trong Git. [W3C SHACL](https://www.w3.org/TR/shacl/) cung cấp
ngữ nghĩa cho target, focus node và báo cáo vi phạm. Đếm focus node giúp phân
biệt shape đã kiểm tra dữ liệu với shape không tìm thấy đối tượng áp dụng.
[OQuaRE](https://research.manchester.ac.uk/en/publications/oquare-a-square-based-approach-for-evaluating-the-quality-of-onto/)
gợi ý tách chất lượng thành các đặc tính có thể đo riêng. Repo dùng ba nhóm
dưới đây và giữ nguyên số đếm để thấy rõ mẫu số và lý do không đạt.

| Nhóm | Phép đo | Điều kiện bắt buộc |
| --- | --- | --- |
| Khai báo schema | Thuộc tính có cả `rdfs:domain` và `rdfs:range` / tổng IRI được khai báo là `owl:ObjectProperty` hoặc `owl:DatatypeProperty`. | Mọi thuộc tính trong mẫu số phải có cả hai khai báo. Báo cáo liệt kê IRI và trường thiếu. |
| SHACL | Conformance trên hợp của các module và dataset mẫu, với cùng cách mở rộng kiểu như `make validate`. Báo cáo ghi focus node, path, severity và loại ràng buộc cho kết quả vi phạm. | Đồ thị phải conform. |
| Phạm vi SHACL | Shape đang bật có ít nhất một focus node theo `sh:targetClass` / tổng shape đang bật khai báo `sh:targetClass`. Gộp các lớp đích, tính cả lớp con và đếm mỗi node một lần trong mỗi shape. | Chỉ dùng để phát hiện khoảng trống fixture; không đặt ngưỡng. Các kiểu target khác vẫn được SHACL validate nhưng không vào mẫu số này. |
| CQ | Số câu `passed` / số câu `Required`; thêm `passed` / tổng câu để biết phạm vi hồi quy của hợp đồng. | Mỗi câu `Required` có đúng một spec cùng ID và nhóm; query phải trả về đúng cột và hàng so với spec. Spec lạ, trùng hoặc gắn vào câu `Backlog` làm gate thất bại. |

Hai phép đo dùng chung dữ liệu mẫu. CQ chạy trên các kiểu được khai báo trực
tiếp, trước khi mở rộng kiểu cho SHACL. Điều này giữ nguyên nghĩa của phép đếm
theo kiểu trong CQ-012. Kết quả CQ-012 xác nhận truy vấn mẫu đếm theo kiểu; các
chiều vùng, thời gian và triển khai OLAP cần bằng chứng riêng.

## Đọc kết quả và bổ sung bằng chứng

Snapshot checked 2026-11-12: 28 classes, **68/68** properties have domain and
range, all **9/9** class-target shapes have focus nodes, and the combined
corpus conforms. The fixture includes assertion, document/source, unresolved
reference, Identifier, ObservationResult, Sensor and Track records.

**41/41** competency questions match their expected rows across the baseline
datasets and the dedicated semantic workload. The added cases exercise
half-open time boundaries, correction and retraction, conflicting location
claims, normalized sources and inline evidence, unresolved identity,
scheme-scoped identifiers, observation-result/measurement separation and track
provenance. See the [CQ contract](../generated/competency_questions.md) and
`benchmarks/expected_results/` for exact cases.

Kết quả đo được mức phù hợp với khai báo, shape và ví dụ trong repo. Chúng
không chứng minh ontology nhất quán theo OWL DL, dữ liệu thực đầy đủ, hoặc
các sự kiện có thật. Chạy [đánh giá thiết kế](ontology-design.md) để kiểm tra
phân cấp lớp và tham chiếu giữa các module. Để đo độ chính xác trên dữ liệu
thực, cần một mẫu đã được chuyên gia xác minh với nguồn và mốc thời gian rõ
ràng; đo precision và recall theo từng loại sự kiện hoặc quan hệ trên tập
đó. Latency và khả năng mở rộng tiếp tục được theo dõi bằng
[SLO riêng](../generated/performance_slo.md).
