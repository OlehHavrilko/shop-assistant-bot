# Фотографии демонстрационного магазина «Полотно»

Проверено 17 октября 2026 года по страницам фотографий и официальной [Pexels License](https://www.pexels.com/license/). Эта лицензия разрешает бесплатное использование и изменение фотографий в приложениях, в том числе коммерческих; указание автора необязательно. Здесь авторы указаны для прослеживаемости. Ограничения включают продажу неизменённых копий, распространение на фотостоках, использование в товарном знаке и создание впечатления поддержки продукта изображёнными людьми или брендами. Фотографии не становятся собственностью проекта и не покрываются лицензией MIT на код.

Файлы входят в демоприложение как иллюстрации карточек, а не как самостоятельная коллекция для продажи или перепубликации на фотостоке. В них нет узнаваемых людей, логотипов или обещаний реального ассортимента.

## Что показывают карточки

Магазин, артикулы, составы, ширина, плотность, применение, цены и остатки вымышлены. Фотографии настоящие, но не являются фотографиями этих вымышленных товаров. Цвет и фактура приблизительные; часть снимков используется для нескольких товаров. В интерфейсе карточки должны содержать пометку «Фото ілюстративне». В частности, фото однотонных тканей не подтверждает состав или двухслойную структуру муслина. Перед использованием проекта для настоящего магазина эти изображения нужно заменить фотографиями соответствующих рулонов.

Все изображения загружены напрямую с CDN Pexels с параметрами `auto=compress&cs=tinysrgb&w=1200&q=78&fit=max`. Файлы JPEG, ширина 1200 пикселей, каждый меньше 500 КБ. Платные API, генерация изображений, снимки экрана и локальное изменение цвета не использовались.

## Источники

| Файл в `examples/media/` | Автор | Оригинальная страница | Загруженный файл | Лицензия |
| --- | --- | --- | --- | --- |
| `linen-ivory.jpg` | Mike Murray | [Close-up of Fabric Textures](https://www.pexels.com/photo/close-up-of-fabric-textures-5908326/) | [JPEG](https://images.pexels.com/photos/5908326/pexels-photo-5908326.jpeg?auto=compress&cs=tinysrgb&w=1200&q=78&fit=max) | [Pexels License](https://www.pexels.com/license/) |
| `linen-natural.jpg` | Monstera Production | [Close-up of a Linen Texture](https://www.pexels.com/photo/close-up-of-a-linen-texture-7794365/) | [JPEG](https://images.pexels.com/photos/7794365/pexels-photo-7794365.jpeg?auto=compress&cs=tinysrgb&w=1200&q=78&fit=max) | [Pexels License](https://www.pexels.com/license/) |
| `linen-white.jpg` | Alina Skazka | [White Linen Fabric](https://www.pexels.com/photo/white-linen-fabric-18444201/) | [JPEG](https://images.pexels.com/photos/18444201/pexels-photo-18444201.jpeg?auto=compress&cs=tinysrgb&w=1200&q=78&fit=max) | [Pexels License](https://www.pexels.com/license/) |
| `cotton-blue.jpg` | Júlio Riccó | [Close-Up Photo of a Blue Fabric Texture](https://www.pexels.com/photo/close-up-photo-of-a-blue-fabric-texture-18372335/) | [JPEG](https://images.pexels.com/photos/18372335/pexels-photo-18372335.jpeg?auto=compress&cs=tinysrgb&w=1200&q=78&fit=max) | [Pexels License](https://www.pexels.com/license/) |
| `cotton-yellow.jpg` | Ivan Babydov | [Yellow Textile in Close-Up Photography](https://www.pexels.com/photo/yellow-textile-in-close-up-photography-7790155/) | [JPEG](https://images.pexels.com/photos/7790155/pexels-photo-7790155.jpeg?auto=compress&cs=tinysrgb&w=1200&q=78&fit=max) | [Pexels License](https://www.pexels.com/license/) |
| `cotton-green.jpg` | Engin Akyurt | [Texture of Green Fabric](https://www.pexels.com/photo/texture-of-green-fabric-11871835/) | [JPEG](https://images.pexels.com/photos/11871835/pexels-photo-11871835.jpeg?auto=compress&cs=tinysrgb&w=1200&q=78&fit=max) | [Pexels License](https://www.pexels.com/license/) |
| `viscose-green.jpg` | YESICA CORONADO | [Photo of a Green Fabric](https://www.pexels.com/photo/photo-of-a-green-fabric-14944459/) | [JPEG](https://images.pexels.com/photos/14944459/pexels-photo-14944459.jpeg?auto=compress&cs=tinysrgb&w=1200&q=78&fit=max) | [Pexels License](https://www.pexels.com/license/) |
| `viscose-pink.jpg` | Sandra Filipe | [Close Up Photo of Pink Fabric](https://www.pexels.com/photo/close-up-photo-of-pink-fabric-6653661/) | [JPEG](https://images.pexels.com/photos/6653661/pexels-photo-6653661.jpeg?auto=compress&cs=tinysrgb&w=1200&q=78&fit=max) | [Pexels License](https://www.pexels.com/license/) |

## Демоданные

`examples/store_catalog.csv` содержит 20 товаров: 5 вариантов льна, 5 хлопка, 5 вискозы и 5 муслина. Первые четыре строки сохраняют факты из прежнего `examples/catalog.csv`, который остаётся отдельным маленьким примером и тестовой фикстурой. Нулевые остатки у `123` и `VIS-023`; неизвестные у `COT-010` и `MUS-034`.

`examples/store.json` содержит публичные описания и привязку фото к тем же 20 SKU. Единственный источник цены, остатка и времени обновления — CSV при импорте в SQLite. Закрытые закупочные цены и поставщики в JSON не включаются. Условия доставки и оплаты — явно демонстрационные, оформление заказов и платежи отсутствуют.
