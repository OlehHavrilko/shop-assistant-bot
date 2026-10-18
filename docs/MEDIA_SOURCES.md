# Фотографії демонстраційного магазину Shop Assistant

Перевірено 17 жовтня 2026 року за сторінками фотографій і офіційною [Pexels License](https://www.pexels.com/license/). Ліцензія дозволяє безкоштовно використовувати й змінювати фото в застосунках, зокрема комерційних; зазначати автора не обов'язково. Автори вказані для простежуваності. Обмеження: не можна продавати незмінені копії, поширювати їх на фотостоках, використовувати в товарному знаку чи створювати враження, що зображені люди або бренди підтримують продукт. Фото не стають власністю проєкту й не підпадають під ліцензію MIT на код.

Файли входять у демозастосунок як ілюстрації карток, а не як окрема колекція для продажу чи перепублікації. На них немає впізнаваних людей, логотипів або обіцянок реального асортименту.

## Що показують картки

Магазин, артикули, склад, ширина, щільність, призначення, ціни й залишки вигадані. Фото справжні, але це не фото цих вигаданих товарів. Колір і фактура приблизні; частина знімків використовується для кількох товарів. Картки в інтерфейсі мають позначку «Фото ілюстративне». Зокрема, фото однотонних тканин не підтверджує склад чи двошарову структуру мусліну. Для справжнього магазину ці зображення треба замінити фото відповідних рулонів.

Усі зображення завантажено напряму з CDN Pexels із параметрами `auto=compress&cs=tinysrgb&w=1200&q=78&fit=max`. Формат JPEG, ширина 1200 пікселів, кожен файл менший за 500 КБ. Платні API, генерація зображень, знімки екрана й локальна зміна кольору не використовувалися.

## Джерела

| Файл у `examples/media/` | Автор | Оригінальна сторінка | Завантажений файл | Ліцензія |
| --- | --- | --- | --- | --- |
| `linen-ivory.jpg` | Mike Murray | [Close-up of Fabric Textures](https://www.pexels.com/photo/close-up-of-fabric-textures-5908326/) | [JPEG](https://images.pexels.com/photos/5908326/pexels-photo-5908326.jpeg?auto=compress&cs=tinysrgb&w=1200&q=78&fit=max) | [Pexels License](https://www.pexels.com/license/) |
| `linen-natural.jpg` | Monstera Production | [Close-up of a Linen Texture](https://www.pexels.com/photo/close-up-of-a-linen-texture-7794365/) | [JPEG](https://images.pexels.com/photos/7794365/pexels-photo-7794365.jpeg?auto=compress&cs=tinysrgb&w=1200&q=78&fit=max) | [Pexels License](https://www.pexels.com/license/) |
| `linen-white.jpg` | Alina Skazka | [White Linen Fabric](https://www.pexels.com/photo/white-linen-fabric-18444201/) | [JPEG](https://images.pexels.com/photos/18444201/pexels-photo-18444201.jpeg?auto=compress&cs=tinysrgb&w=1200&q=78&fit=max) | [Pexels License](https://www.pexels.com/license/) |
| `cotton-blue.jpg` | Júlio Riccó | [Close-Up Photo of a Blue Fabric Texture](https://www.pexels.com/photo/close-up-photo-of-a-blue-fabric-texture-18372335/) | [JPEG](https://images.pexels.com/photos/18372335/pexels-photo-18372335.jpeg?auto=compress&cs=tinysrgb&w=1200&q=78&fit=max) | [Pexels License](https://www.pexels.com/license/) |
| `cotton-yellow.jpg` | Ivan Babydov | [Yellow Textile in Close-Up Photography](https://www.pexels.com/photo/yellow-textile-in-close-up-photography-7790155/) | [JPEG](https://images.pexels.com/photos/7790155/pexels-photo-7790155.jpeg?auto=compress&cs=tinysrgb&w=1200&q=78&fit=max) | [Pexels License](https://www.pexels.com/license/) |
| `cotton-green.jpg` | Engin Akyurt | [Texture of Green Fabric](https://www.pexels.com/photo/texture-of-green-fabric-11871835/) | [JPEG](https://images.pexels.com/photos/11871835/pexels-photo-11871835.jpeg?auto=compress&cs=tinysrgb&w=1200&q=78&fit=max) | [Pexels License](https://www.pexels.com/license/) |
| `viscose-green.jpg` | YESICA CORONADO | [Photo of a Green Fabric](https://www.pexels.com/photo/photo-of-a-green-fabric-14944459/) | [JPEG](https://images.pexels.com/photos/14944459/pexels-photo-14944459.jpeg?auto=compress&cs=tinysrgb&w=1200&q=78&fit=max) | [Pexels License](https://www.pexels.com/license/) |
| `viscose-pink.jpg` | Sandra Filipe | [Close Up Photo of Pink Fabric](https://www.pexels.com/photo/close-up-photo-of-pink-fabric-6653661/) | [JPEG](https://images.pexels.com/photos/6653661/pexels-photo-6653661.jpeg?auto=compress&cs=tinysrgb&w=1200&q=78&fit=max) | [Pexels License](https://www.pexels.com/license/) |

## Демодані

`examples/store_catalog.csv` містить 20 товарів: по 5 варіантів льону, бавовни, віскози й мусліну. Перші чотири рядки зберігають факти з попереднього `examples/catalog.csv`, який лишається окремим маленьким прикладом і тестовою фікстурою. Нульові залишки — у `123` і `VIS-023`; невідомі — у `COT-010` і `MUS-034`.

`examples/store.json` містить публічні описи й прив'язку фото до тих самих 20 SKU. Єдине джерело ціни, залишку й часу оновлення — CSV під час імпорту в SQLite. Закриті закупівельні ціни й постачальники в JSON не потрапляють. Умови доставки й оплати демонстраційні; оформлення замовлень і платежів немає.
