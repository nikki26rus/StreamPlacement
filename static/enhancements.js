/* Adds explicit edit actions after the dynamic cabinet sections render. */
const enhanceCabinet = async () => {
  document.querySelectorAll(".del-custom:not([data-enhanced])").forEach((remove) => {
    remove.dataset.enhanced = "1";
    const edit = document.createElement("button");
    edit.className = "ghost small";
    edit.textContent = "Изменить";
    edit.onclick = async () => {
      try {
        const data = await api(`/api/chats/${state.chat}/buttons`);
        const index = Number(remove.dataset.index);
        const current = data.custom_buttons[index];
        if (!current) return;
        const label = prompt("Название кнопки", current.label);
        if (label === null) return;
        const url = prompt("Ссылка", current.url);
        if (url === null) return;
        const group = prompt("Группа (1–20)", current.group);
        if (group === null) return;
        const emoji = prompt("Unicode emoji", current.emoji || "");
        if (emoji === null) return;
        await api(`/api/chats/${state.chat}/buttons/custom/${index}`, {
          method: "PUT",
          body: JSON.stringify({
            label,
            url,
            group: Number(group),
            emoji,
            style: data.button_styles[`custom:${index}`] || null,
          }),
        });
        toast("Кнопка изменена");
        buttons();
      } catch (error) {
        toast(error.message, true);
      }
    };
    remove.before(edit);
  });

  document.querySelectorAll(".del-event:not([data-enhanced])").forEach((remove) => {
    remove.dataset.enhanced = "1";
    const edit = document.createElement("button");
    edit.className = "ghost small";
    edit.style.cssText = "float:right;margin-right:4px";
    edit.textContent = "✎";
    edit.title = "Изменить";
    edit.onclick = async () => {
      try {
        const scheduleData = await api(`/api/chats/${state.chat}/schedule`);
        const current = scheduleData.items.find((item) => item.id === Number(remove.dataset.id));
        if (!current) return;
        const weekday = prompt("День недели: 0 — пн, 6 — вс", current.weekday);
        if (weekday === null) return;
        const time = prompt("Время (HH:MM)", current.time);
        if (time === null) return;
        const title = prompt("Название", current.title);
        if (title === null) return;
        const description = prompt("Описание", current.description);
        if (description === null) return;
        await api(`/api/chats/${state.chat}/schedule/items/${current.id}`, {
          method: "PUT",
          body: JSON.stringify({weekday: Number(weekday), time, title, description}),
        });
        toast("Пункт расписания изменён");
        schedule();
      } catch (error) {
        toast(error.message, true);
      }
    };
    remove.before(edit);
  });
};

new MutationObserver(() => enhanceCabinet()).observe(document.body, {childList: true, subtree: true});
