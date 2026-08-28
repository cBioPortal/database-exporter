import { loadPublicData } from "./data";
import { renderIcons, renderLoadError, renderPage } from "./render";
import "./style.css";

renderIcons();

loadPublicData()
  .then(({ dumps, huggingFace, assetBaseUrl }) => {
    renderPage(dumps, huggingFace, assetBaseUrl);
  })
  .catch(renderLoadError);
