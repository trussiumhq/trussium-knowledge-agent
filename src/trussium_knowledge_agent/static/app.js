"use strict";

const form = document.querySelector("#question-form");
const questionField = document.querySelector("#question");
const limitField = document.querySelector("#limit");
const submitButton = document.querySelector("#submit");
const statusMessage = document.querySelector("#status");
const resultPanel = document.querySelector("#result");
const resultHeading = document.querySelector("#result-heading");
const answerText = document.querySelector("#answer");
const citationRegion = document.querySelector("#citation-region");
const citationList = document.querySelector("#citations");

function clearResult() {
  resultPanel.hidden = true;
  citationRegion.hidden = true;
  citationList.replaceChildren();
  answerText.textContent = "";
}

function showCitations(citations) {
  citationList.replaceChildren();
  for (const citation of citations) {
    const item = document.createElement("li");
    const location = document.createElement("span");
    const heading = document.createElement("span");
    location.className = "citation-location";
    location.textContent = `${citation.relative_path}#${citation.heading_anchor}`;
    heading.className = "citation-heading";
    heading.textContent = citation.heading_path.join(" › ") || "Document";
    item.append(location, heading);
    citationList.append(item);
  }
  citationRegion.hidden = citations.length === 0;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearResult();
  statusMessage.classList.remove("error");
  statusMessage.textContent = "Searching indexed sources and preparing a grounded answer…";
  submitButton.disabled = true;

  try {
    const response = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ question: questionField.value, limit: Number(limitField.value) }),
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(typeof payload.detail === "string" ? payload.detail : "The request failed.");
    }

    answerText.textContent = payload.answer;
    showCitations(payload.citations);
    resultHeading.textContent = payload.status === "answered" ? "Answer" : "Not enough evidence";
    resultPanel.hidden = false;
    statusMessage.textContent = payload.status === "answered"
      ? "Answer generated from retrieved evidence."
      : "Try indexing more relevant documentation or rephrasing the question.";
  } catch (error) {
    statusMessage.classList.add("error");
    statusMessage.textContent = error instanceof Error
      ? error.message
      : "The question could not be completed. Please try again.";
  } finally {
    submitButton.disabled = false;
  }
});
