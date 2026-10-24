# Reopening saved files before evaluation

The evaluator can reopen an explicitly declared saved file when its document window is absent.
This permits file-based evaluation after an agent saves and closes a document.
It does not infer success, reconstruct unsaved work, or select another output file.

`activate_window.parameters.open_file_if_missing` is an optional, nonempty guest file path.
It requires `strict: true` and title-based activation.
The controller calls `/setup/open_file` once, only after the exact missing-window HTTP 404 response.
The guest checks that the path exists before opening it with its default application.
The controller then repeats the original strict activation within the same 30-second deadline.
An existing window follows the original save sequence without reopening.
Missing files, open failures, wrong titles, transport failures, and uncertain activation results remain errors.
Subsequent save commands and file evaluation run only after activation succeeds.

This is evaluator preparation. The agent instruction, initial setup, expected artifacts, metrics,
and score logic are unchanged. No evaluator information is sent to the agent.
A wrong or incomplete saved file still reaches the original metric as that same output file.
A save to a different path does not replace the declared path.

## Configuration audit

The 2026-10-24 audit covers all 141 tasks selected by
`src/win-arena-container/client/evaluation_examples_windows/test_all.json`.
It adds paths to 38 tasks: 22 Calc tasks and 16 Writer tasks.
Each has one strict document-title activation, a save-only postconfig,
and one unique `vm_file` path identical to its original `open` path.
The other 103 tasks retain their previous preparation: 19 have activation and 84 do not.
The table lists every changed task and its declared filename;
its full path is explicit in that task's JSON configuration.

| Application | Task ID | File |
| --- | --- | --- |
| libreoffice_writer | `0e763496-b6bb-4508-a427-fad0b6c3e195-WOS` | `Dublin_Zoo_Intro.docx` |
| libreoffice_writer | `0a0faba3-5580-44df-965d-f562a99b291c-WOS` | `04 CHIN9505 EBook Purchasing info 2021 Jan.docx` |
| libreoffice_writer | `936321ce-5236-426a-9a20-e0e3c5dc536f-WOS` | `Graphemes_Sound_Letter_Patterns.docx` |
| libreoffice_writer | `d53ff5ee-3b1a-431e-b2be-30ed2673079b-WOS` | `presentation_instruction_2023_Feb.docx` |
| libreoffice_writer | `e246f6d8-78d7-44ac-b668-fcf47946cb50-WOS` | `Y22-2119-assign4.docx` |
| libreoffice_writer | `72b810ef-4156-4d09-8f08-a0cf57e7cefe-WOS` | `GEOG2169_Course_Outline_2022-23.docx` |
| libreoffice_writer | `41c621f7-3544-49e1-af8d-dafd0f834f75-WOS` | `How_to_read_a_scientific_article.docx` |
| libreoffice_writer | `88fe4b2d-3040-4c70-9a70-546a47764b48-WOS` | `CCCH9003_Tutorial_guidelines.docx` |
| libreoffice_writer | `b21acd93-60fd-4127-8a43-2f5178f4a830-WOS` | `CCHU9045_Course_Outline_2019-20.docx` |
| libreoffice_writer | `0810415c-bde4-4443-9047-d5f70165a697-WOS` | `Novels_Intro_Packet.docx` |
| libreoffice_writer | `e528b65e-1107-4b8c-8988-490e4fece599-WOS` | `Geography_And_Magical_Realism.docx` |
| libreoffice_writer | `0b17a146-2934-46c7-8727-73ff6b6483e8-WOS` | `H2O_Factsheet_WA.docx` |
| libreoffice_writer | `0e47de2a-32e0-456c-a366-8c607ef7a9d2-WOS` | `LibreOffice_Open_Source_Word_Processing.docx` |
| libreoffice_writer | `6a33f9b9-0a56-4844-9c3f-96ec3ffb3ba2-WOS` | `sample-recruitment-phone-script.odt` |
| libreoffice_writer | `3ef2b351-8a84-4ff2-8724-d86eae9b842e-WOS` | `Constitution_Template_With_Guidelines.docx` |
| libreoffice_writer | `adf5e2c3-64c7-4644-b7b6-d2f0167927e7-WOS` | `Essay_Writing_English_for_uni.docx` |
| libreoffice_calc | `04d9aeaf-7bed-4024-bedb-e10e6f00eb7f-WOS` | `SmallBalanceSheet.xlsx` |
| libreoffice_calc | `8b1ce5f2-59d2-4dcc-b0b0-666a714b9a14-WOS` | `Calendar_Highlight_Weekend_Days.xlsx` |
| libreoffice_calc | `1d17d234-e39d-4ed7-b46f-4417922a4e7c-WOS` | `FutureValue.xlsx` |
| libreoffice_calc | `0cecd4f3-74de-457b-ba94-29ad6b5dafb6-WOS` | `copy_sheet_insert.xlsx` |
| libreoffice_calc | `26a8440e-c166-4c50-aef4-bfb77314b46b-WOS` | `SalesRep.xlsx` |
| libreoffice_calc | `0acbd372-ca7a-4507-b949-70673120190f-WOS` | `NetIncome.xlsx` |
| libreoffice_calc | `01b269ae-2111-4a07-81fd-3fcd711993b0-WOS` | `Student_Level_Fill_Blank.xlsx` |
| libreoffice_calc | `7e429b8d-a3f0-4ed0-9b58-08957d00b127-WOS` | `VLOOKUP_Fill_the_form.xlsx` |
| libreoffice_calc | `1e8df695-bd1b-45b3-b557-e7d599cf7597-WOS` | `WeeklySales.xlsx` |
| libreoffice_calc | `30e3e107-1cfb-46ee-a755-2cd080d7ba6a-WOS` | `DemographicProfile.xlsx` |
| libreoffice_calc | `0a2e43bf-b26c-4631-a966-af9dfa12c9e5-WOS` | `SalesRep.xlsx` |
| libreoffice_calc | `4de54231-e4b5-49e3-b2ba-61a0bec721c0-WOS` | `RampUpAndDown.xlsx` |
| libreoffice_calc | `4e6fcf72-daf3-439f-a232-c434ce416af6-WOS` | `Employee_Age_By_Birthday.xlsx` |
| libreoffice_calc | `1de60575-bb6e-4c3d-9e6a-2fa699f9f197-WOS` | `EntireSummerSales.xlsx` |
| libreoffice_calc | `7a4e4bc8-922c-4c84-865c-25ba34136be1-WOS` | `Name_Order_Id_move_column.xlsx` |
| libreoffice_calc | `21ab7b40-77c2-4ae6-8321-e00d3a086c73-WOS` | `PeriodRate.xlsx` |
| libreoffice_calc | `39aa4e37-dc91-482e-99af-132a612d40f3-WOS` | `LARS_Science_Assessment_Resource_List_7-30-21.xlsx` |
| libreoffice_calc | `5d353deb-c4b0-4126-a99e-5490817b48cb-WOS` | `EasyGDPBreakdown.xlsx` |
| libreoffice_calc | `035f41ba-6653-43ab-aa63-c86d449d62e5-WOS` | `IncomeStatement2.xlsx` |
| libreoffice_calc | `7efeb4b1-3d19-4762-b163-63328d66303b-WOS` | `Order_Sales_Serial#.xlsx` |
| libreoffice_calc | `9ed02102-6b28-4946-8339-c028166e9512-WOS` | `BoomerangSales.xlsx` |
| libreoffice_calc | `5f8601f8-6e90-4d2c-91bb-eb5836ad1d5c-WOS` | `ExpenseReport.xlsx` |

Configurations with activation that retain their previous behavior:

| Application | Task ID | Reason |
| --- | --- | --- |
| file_explorer | `e27984c7-968c-48d7-b2c3-6e45cdcc5249-WOS` | Folder or live Explorer state; no document save sequence. |
| file_explorer | `5548314e-d807-4e9e-97e9-b3a4f9fd634f-WOS` | Folder or live Explorer state; no document save sequence. |
| file_explorer | `34a4fee9-e52e-4a4a-96d2-68d35091504a-WOS` | Folder or live Explorer state; no document save sequence. |
| file_explorer | `7c70e16b-e14f-4baa-b046-3e022b2d0305-WOS` | Folder or live Explorer state; no document save sequence. |
| file_explorer | `0c9dda13-428c-492b-900b-f48562111f93-WOS` | Folder or live Explorer state; no document save sequence. |
| file_explorer | `016c9a9d-f2b9-4428-8fdb-f74f4439ece6-WOS` | Folder or live Explorer state; no document save sequence. |
| file_explorer | `2b0c0844-bd4f-42ba-a25e-afb4267d51e2-WOS` | Folder or live Explorer state; no document save sequence. |
| microsoft_paint | `15f8de6e-3d39-40e4-af17-bdbb2393c0d9-WOS` | Requires live canvas or Save As interaction. |
| microsoft_paint | `44dbac63-32bf-4cd2-81b4-ad6803ec812d-WOS` | Requires live canvas or Save As interaction. |
| libreoffice_writer | `f178a4a9-d090-4b56-bc4c-4b72a61a035d-WOS` | Getter reads application settings, not the opened document. |
| libreoffice_calc | `0bf05a7d-b28b-44d2-955a-50b41e24012a-WOS` | Postconfig also converts files; outside the save-only contract. |
| libreoffice_calc | `4f07fbe9-70de-4927-a4d5-bb28bc12c52c-WOS` | Postconfig also converts files; outside the save-only contract. |
| vs_code | `ec71221e-ac43-46f9-89b8-ee7d80f7e1c5-WOS` | Uses explicit `code` launch and non-strict activation; default file association cannot guarantee the editor. |
| vs_code | `0ed39f63-6049-43d4-ba4d-5fa2fe04a951-WOS` | Uses explicit `code` launch and non-strict activation; default file association cannot guarantee the editor. |
| clock | `02F10F89-7171-4D37-8550-A00BA8930CDF-WOS` | Requires live application state. |
| clock | `02F10F89-7171-4D37-8550-A00BA8930CDF-2-WOS` | Requires live application state. |
| clock | `91A30BE9-0E11-4374-8D43-41D4D097080A-WOS-2` | Requires live application state. |
| clock | `91A30BE9-0E11-4374-8D43-41D4D097080A-WOS` | Requires live application state. |
| notepad | `366de66e-cbae-4d72-b042-26390db2b145-WOS` | Postconfig already opens the declared file. |

## Validation

On 2026-10-24, 27 host tests passed using the existing WAA Python environment.
They cover reopening before save, existing windows, absent files, open failures, wrong titles,
unknown activation failures, deadline accounting, opt-in validation, and the full selected configuration set.
A semantic comparison against the prerequisite branch confirms that the 141 task documents differ
only by the 38 evaluator activation parameters. Existing setup and task Python syntax checks also pass.
These host tests mock the guest endpoints.

A separate Windows regression passed on 2026-10-24 with source reference `62e3295`:
`waa-postconfig-neutral-reopen-20261024-a1` used a new synthetic XLSX document.
The original save sequence persisted a GUI edit that was previously absent from the file.
After closing the document, the activation endpoint returned the exact missing-window HTTP 404.
The new sequence opened the declared file once, confirmed strict activation, and saved it.
File inspection confirmed that the edited cell and an untouched sentinel cell survived.
The reopening screenshot also showed those values in Calc.
An absent-file case returned HTTP 404 from opening and never reached the save command.
The regression closed its document afterward and confirmed the final missing-window response.

The evidence directory for this attempt contains `report.json`, `controller.log`, `canary.py`,
and closed, reopened, and released screenshots. This test used no reference answers or task metrics.
It validates the mechanism in Calc; it does not establish a task score or a Writer application regression.
Keep frozen runs and their recorded scores unchanged.

```sh
python -m unittest discover -s tests -v
```

