# Đánh giá thiết kế ontology

`make ontology-design` kiểm tra các module Turtle dưới `ontology/` và ghi
`build/ontology-design.json`. Chỉ cần môi trường Python của repo; chương
trình không tải import qua mạng. `make check` chạy bước này và GitHub Actions
lưu báo cáo đã tạo dưới tên `ontology-design`.

```bash
make ontology-design
jq '{passed, modules, errors, warnings}' build/ontology-design.json
```

Mã thoát của `tools/check_ontology_design.py`: **0** khi không có lỗi chặn,
**1** khi báo cáo ghi nhận lỗi thiết kế, **2** khi không thể chạy đánh giá
(chẳng hạn Turtle hỏng hoặc header không xác định được chủ sở hữu namespace).

## Phương pháp và cách đọc kết quả

[OOPS!](https://oa.upm.es/35873/) cung cấp các mẫu lỗi thường gặp như chu
trình phân cấp và thiếu annotation. [Ngữ nghĩa OWL 2](https://www.w3.org/TR/owl2-semantics/)
giải thích vì sao một lớp kế thừa hai lớp rời nhau không thể có cá thể.
[OntoClean](https://doi.org/10.1145/503124.503150) giúp người review đặt câu hỏi
về bản chất và vai trò của các lớp; bước Python không tự suy ra những thuộc tính này.

| Quy tắc | Điều kiện |
| --- | --- |
| `subclass-cycle` | Chặn cạnh tự trỏ hoặc chu trình trong các cạnh `rdfs:subClassOf` được khai báo giữa IRI. Đây là quy ước thiết kế của repo; OWL có thể diễn giải chu trình là tương đương. |
| `empty-class` | Chặn lớp đi tới `owl:Nothing` hoặc kế thừa hai lớp rời nhau. Kiểm tra cả đường đi qua tương đương giữa lớp có IRI và ghi lại các tiên đề dẫn tới kết quả. |
| `unresolved-import`, `undeclared-term`, `wrong-term-kind`, `missing-import` | Chặn import không có đích cục bộ, term nội bộ thiếu khai báo hoặc sai loại, và term ngoài phạm vi import. Kiểm tra các vị trí có IRI trong tiên đề phân cấp, tương đương, rời nhau, nghịch đảo và `domain`/`range` của thuộc tính dữ liệu hay đối tượng. `make dag` vẫn kiểm tra hướng tầng và chu trình import. |
| `required-subclass`, `required-disjoint` | Chặn việc mất các quyết định phân loại bên dưới. |
| `missing-label`, `missing-description`, `inverse-signature` | Cảnh báo term không có `rdfs:label` hoặc `rdfs:comment` dạng literal không rỗng, hoặc cặp nghịch đảo không có hai bộ `domain`/`range` khai báo đối xứng. Sai khác chữ ký có thể có chủ ý; đọc bằng chứng trước khi sửa. |

Số liệu annotation dùng các IRI lớp và thuộc tính đối tượng/dữ liệu được
khai báo trong namespace của module làm mẫu số. Mỗi term chỉ đếm một lần.
Lần chạy đầu tiên: **7 module**, **0 lỗi**, **70/70** term có nhãn,
**34/70** term có mô tả. Cả **36** term thiếu mô tả thuộc core; ưu tiên viết
định nghĩa cho term đang dùng trước khi mở rộng vocabulary.

## Các quyết định cần bảo vệ

| Bất biến | Vì sao |
| --- | --- |
| `Observation ⊑ Event` | Hành động quan sát xảy ra tại một thời điểm. Nó khác với thông tin rút ra từ hành động đó. |
| `Track ⊑ InformationObject` | Track là giả thuyết có thể chỉnh sửa về chuyển động của một thực thể. Xem [ADR-0005](../adr/ADR-0005-tracking-information-object.md). |
| `Assertion ⊑ InformationObject`, `Document ⊑ Source` | Một phát biểu biểu diễn điều được khẳng định; tài liệu là nguồn chứng cứ. Sự kiện ghi phát biểu có vòng đời riêng. Xem [kiến trúc](../architecture.md). |
| `Event disjointWith InformationObject` | Bảo vệ ranh giới giữa điều xảy ra và điều được ghi lại, kể cả khi một lớp mới vô tình kế thừa cả hai nhánh. |

Khi thay đổi nghĩa của lớp hoặc quan hệ, ghi một ví dụ đúng và một phản ví dụ
trong mô tả PR. Với quan hệ lớp con, hỏi liệu **mọi** cá thể của lớp con có
luôn thuộc lớp cha không. Chẳng hạn, một người làm chỉ huy có quan hệ với
vai trò chỉ huy; việc đảm nhiệm vai trò không biến người đó thành một cá thể
thuộc lớp `Role`. Nếu thay đổi một bất biến ở trên, cập nhật ADR hoặc quyết
định kiến trúc, test và kế hoạch migration tương ứng.

Báo cáo chỉ kiểm tra phần được hỗ trợ của các tiên đề giữa IRI. Nó không
phân tích biểu thức lớp phức tạp, không xác nhận tính nhất quán OWL 2 DL của
toàn bộ ontology và không đánh giá dữ liệu thực. Xem
[đánh giá chất lượng](ontology-quality.md) cho SHACL và câu hỏi nghiệp vụ
trên dữ liệu mẫu.
